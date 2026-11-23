/* Wili -- display CPU, core 0.
 *
 * Turns the FreeWili OG into a voice-assistant head for a host computer:
 *   - streams the PDM microphone to the host (8 kHz int16) on request,
 *   - plays int16 audio the host streams to it through the I2S speaker,
 *   - reports button presses, the accelerometer (100 Hz) and the charger's
 *     battery/USB-power readings (every 2 s),
 *   - drives the LED bar from the face's mood,
 *   - hands mood requests, audio envelopes and a caption line (e.g. a sleep
 *     countdown) to the face on core 1,
 *   - and, on its own, turns the face to match how the board is held, and
 *     (with no host connected) sleeps while it lies face down.
 * Protocol: wili_proto.h. Power: red held 6 s -> ship mode, as every app here.
 */
#include <string.h>

#include "face.h"
#include "fwog_display.h"
#include "hardware/pio.h"
#include "pico/multicore.h"
#include "pico/stdio_usb.h"
#include "pico/stdlib.h"
#include "tusb.h"
#include "wili_proto.h"

FWOG_POWER_DEFAULT();

#define WILI_FW_VERSION "009"

/* =========================================================== framing */

static uint8_t crc8(const uint8_t *p, size_t n) {
    uint8_t c = 0;
    while (n--) {
        c ^= *p++;
        for (int i = 0; i < 8; i++) c = (c & 0x80u) ? (uint8_t)((c << 1) ^ 0x07u) : (uint8_t)(c << 1);
    }
    return c;
}

static uint8_t s_tx[6u + WILI_MAX_PAYLOAD];

/* droppable: skip rather than block when the host isn't keeping up -- a
   blocked write stalls this loop for up to 0.5 s (stdio_usb timeout), which
   would starve the speaker and the mic. */
static bool send_frame(uint8_t type, const void *payload, uint16_t len, bool droppable) {
    if (len > WILI_MAX_PAYLOAD || !stdio_usb_connected()) return false;
    const uint32_t total = 6u + len;
    if (droppable && tud_cdc_write_available() < total) return false;
    s_tx[0] = WILI_MAGIC0;
    s_tx[1] = WILI_MAGIC1;
    s_tx[2] = type;
    s_tx[3] = (uint8_t)(len & 0xFFu);
    s_tx[4] = (uint8_t)(len >> 8);
    if (len) memcpy(&s_tx[5], payload, len);
    s_tx[5u + len] = crc8(&s_tx[2], 3u + len);
    stdio_put_string((const char *)s_tx, (int)total, false, false);
    return true;
}

/* =========================================================== speaker */

#define RING_N 16384u                  /* 2.05 s at 8 kHz; power of two */
#define PREBUFFER 2400u                /* 300 ms: covers both 1024-sample refills */
static int16_t s_ring[RING_N];
static uint32_t s_wr, s_rd;            /* monotonic sample counters */
static bool s_spk_active, s_spk_end;
static uint32_t s_spk_played;          /* samples handed to the DAC, since boot */
static uint32_t s_spk_received;        /* samples received from the host, since boot */
static uint32_t s_spk_dropped;         /* received but no room in the ring */
static uint32_t s_spk_last_active_ms;
static uint16_t s_underruns;
static uint32_t s_spk_empty_since;
static bool s_audio_ok;

/* Lip sync. A refilled buffer starts playing about one buffer later
   (128 ms), so each 16 ms block's level is queued with that play time and
   released to the face when it is actually audible. */
#define ENV_BLOCK 128u
#define ENV_Q 32u
static struct { uint32_t at_ms; uint8_t level; } s_envq[ENV_Q];
static uint32_t s_env_head, s_env_tail;

static uint8_t block_level(const int16_t *s, unsigned n) {
    if (!n) return 0;
    uint32_t acc = 0;
    for (unsigned i = 0; i < n; i++) acc += (uint32_t)(s[i] < 0 ? -s[i] : s[i]);
    const uint32_t mean = acc / n;
    const uint32_t lvl = mean * 255u / 5000u;  /* ordinary TTS speech ~ 2-4k */
    return (uint8_t)(lvl > 255u ? 255u : lvl);
}

static unsigned spk_fill(int16_t *out, unsigned cap) {
    const uint32_t used = s_wr - s_rd;
    const unsigned n = used < cap ? (unsigned)used : cap;
    for (unsigned i = 0; i < n; i++) out[i] = s_ring[(s_rd + i) & (RING_N - 1u)];
    s_rd += n;
    s_spk_played += n;
    if (n < cap && !s_spk_end && n > 0) s_underruns++;

    const uint32_t now = to_ms_since_boot(get_absolute_time());
    const uint32_t start = now + (cap * 1000u) / 8000u;
    for (unsigned b = 0; b * ENV_BLOCK < cap; b++) {
        const unsigned off = b * ENV_BLOCK;
        const unsigned m = off < n ? (n - off < ENV_BLOCK ? n - off : ENV_BLOCK) : 0;
        if (s_env_head - s_env_tail >= ENV_Q) s_env_tail++;  /* drop oldest */
        s_envq[s_env_head % ENV_Q].at_ms = start + (off * 1000u) / 8000u;
        s_envq[s_env_head % ENV_Q].level = m ? block_level(out + off, m) : 0;
        s_env_head++;
    }
    return n;
}

static void spk_flush(void) {
    if (s_spk_active) i2s_audio_stop();
    s_spk_active = false;
    s_spk_end = false;
    s_rd = s_wr;
    s_env_tail = s_env_head;
    g_face.mouth = 0;
}

static void spk_push(const uint8_t *p, uint16_t len) {
    const uint32_t n = len / 2u;
    const uint32_t free_n = RING_N - (s_wr - s_rd);
    const uint32_t take = n < free_n ? n : free_n;
    for (uint32_t i = 0; i < take; i++) {
        s_ring[(s_wr + i) & (RING_N - 1u)] = (int16_t)(p[2u * i] | (p[2u * i + 1u] << 8));
    }
    s_wr += take;
    s_spk_received += n;
    s_spk_dropped += n - take;
    if (!s_spk_active) s_spk_end = false;   /* a new utterance */
}

/* Earcons, synthesised straight into the ring so they play at once (END is
   set, which skips the prebuffer). Two short tones with a raised-cosine
   envelope so they don't click. Sine from a 64-entry quarter table. */
static int16_t sine_q15(uint32_t phase /* 0..2^16 = one cycle */) {
    static const int16_t q[65] = {
        0, 804, 1608, 2410, 3212, 4011, 4808, 5602, 6393, 7179, 7962, 8739, 9512,
        10278, 11039, 11793, 12539, 13279, 14010, 14732, 15446, 16151, 16846,
        17530, 18204, 18868, 19519, 20159, 20787, 21403, 22005, 22594, 23170,
        23731, 24279, 24811, 25329, 25832, 26319, 26790, 27245, 27683, 28105,
        28510, 28898, 29268, 29621, 29956, 30273, 30571, 30852, 31113, 31356,
        31580, 31785, 31971, 32137, 32285, 32412, 32521, 32609, 32678, 32728,
        32757, 32767};
    const uint32_t p = phase & 0xFFFFu;
    const uint32_t quad = p >> 14, idx = (p >> 8) & 63u;
    switch (quad) {
    case 0: return q[idx];
    case 1: return q[64u - idx];
    case 2: return (int16_t)-q[idx];
    default: return (int16_t)-q[64u - idx];
    }
}

static void tone_into_ring(uint32_t hz, uint32_t ms, int16_t amp) {
    const uint32_t n = 8u * ms;
    const uint32_t step = (hz << 16) / 8000u;
    uint32_t ph = 0;
    for (uint32_t i = 0; i < n && (s_wr - s_rd) < RING_N; i++, ph += step) {
        /* raised-cosine attack/release of 8 ms each */
        int32_t env = 32767;
        const uint32_t ramp = 64u;
        if (i < ramp) env = (int32_t)(32767u * i / ramp);
        else if (n - i < ramp) env = (int32_t)(32767u * (n - i) / ramp);
        const int32_t s = ((int32_t)sine_q15(ph) * amp) >> 15;
        s_ring[s_wr & (RING_N - 1u)] = (int16_t)((s * env) >> 15);
        s_wr++;
    }
}

static void silence_into_ring(uint32_t ms) {
    for (uint32_t i = 0; i < 8u * ms && (s_wr - s_rd) < RING_N; i++) s_ring[(s_wr++) & (RING_N - 1u)] = 0;
}

static void spk_chime(uint8_t id) {
    switch (id) {
    /* Amplitudes halved after measuring: at 9000 the listen chime hit ~31k in
       the mic and its tail stayed above the speech threshold for ~0.7 s. */
    case WILI_CHIME_LISTEN:
        tone_into_ring(784, 70, 4500); silence_into_ring(25); tone_into_ring(1175, 90, 4500);
        break;
    case WILI_CHIME_DONE:
        tone_into_ring(1047, 60, 3500); silence_into_ring(20); tone_into_ring(698, 80, 3500);
        break;
    case WILI_CHIME_ERROR:
        tone_into_ring(233, 120, 5000); silence_into_ring(40); tone_into_ring(196, 160, 5000);
        break;
    default:
        return;
    }
    silence_into_ring(30);
    s_spk_end = true;   /* play now, no prebuffer wait */
}

static void spk_service(uint32_t now) {
    if (!s_audio_ok) return;
    const uint32_t used = s_wr - s_rd;
    if (!s_spk_active && used > 0 && (used >= PREBUFFER || s_spk_end)) {
        s_spk_active = i2s_audio_start_stream(spk_fill);
        s_spk_empty_since = 0;
    }
    i2s_audio_process();
    if (s_spk_active) {
        if (used == 0) {
            if (!s_spk_empty_since) s_spk_empty_since = now;
            /* after END, let the two DMA buffers (256 ms) finish; without END
               (host vanished mid-utterance) give it 3 s */
            const uint32_t hold = s_spk_end ? 300u : 3000u;
            if (now - s_spk_empty_since > hold) {
                i2s_audio_stop();
                s_spk_active = false;
                s_spk_end = false;
            }
        } else {
            s_spk_empty_since = 0;
        }
    }
    /* release envelope levels that are now audible */
    while (s_env_tail != s_env_head && (int32_t)(now - s_envq[s_env_tail % ENV_Q].at_ms) >= 0) {
        g_face.mouth = s_envq[s_env_tail % ENV_Q].level;
        s_env_tail++;
    }
    if (!s_spk_active) g_face.mouth = 0;
}

/* =========================================================== mic */

static bool s_mic_ok, s_mic_stream;
static fwog_cic_t s_cic;
static int16_t s_pcm[PDM_SAMPLE_BUFFER_SIZE];
static int32_t s_dc_x1, s_dc_y1;

/* Streaming one-pole DC blocker, continuous across buffers (the BSP's
   fwog_dcblock_inplace() restarts every block, which clicks in a stream). */
/* The CIC maps full-scale PDM to 16384 with no gain, so ordinary speech at a
   metre is only tens of counts. +30 dB (x32) puts room noise near ~100 and
   speech in the thousands -- the levels the legacy firmware delivered. */
#define MIC_GAIN_SHIFT 5

static void dcblock(int16_t *s, unsigned n) {
    for (unsigned i = 0; i < n; i++) {
        const int32_t x = (int32_t)s[i] << 8;
        const int32_t y = x - s_dc_x1 + ((s_dc_y1 * 32604) >> 15);
        s_dc_x1 = x;
        s_dc_y1 = y;
        int32_t o = (y >> 8) << MIC_GAIN_SHIFT;
        if (o > 32767) o = 32767;
        if (o < -32768) o = -32768;
        s[i] = (int16_t)o;
    }
}

static void mic_service(void) {
    if (!s_mic_ok) return;
    const uint8_t *raw;
    size_t len;
    if (!pdm_mic_take_raw_buffer(&raw, &len)) return;
    const size_t n = pdm_mic_decode(&s_cic, raw, len, s_pcm);
    if (!n) return;
    dcblock(s_pcm, (unsigned)n);
    uint32_t acc = 0;
    for (size_t i = 0; i < n; i++) acc += (uint32_t)(s_pcm[i] < 0 ? -s_pcm[i] : s_pcm[i]);
    const uint32_t lvl = (acc / n) * 255u / 2500u;
    g_face.mic = (uint8_t)(lvl > 255u ? 255u : lvl);
    if (s_mic_stream) send_frame(WILI_EV_MIC, s_pcm, (uint16_t)(n * 2u), true);
}

/* =========================================================== LEDs */

static bool s_leds_ok, s_led_custom;
static uint8_t s_led_rgb[FWOG_LED_COUNT][3];

/* Global LED brightness, 0..255, applied to everything the app draws (the
   BSP's power-off countdown is separate). The mood colours below are designed
   at full scale; at 255 the bar was blinding on a desk, so the default is
   about a quarter. The host can change it with WILI_CMD_LED_LEVEL. */
static uint8_t s_led_level = 64;

static unsigned dim(unsigned v) {
    if (v > 255) v = 255;
    if (v == 0 || s_led_level == 0) return 0;
    const unsigned d = v * s_led_level / 255u;
    return d ? d : 1u;   /* keep faint glows (idle breathing) visible */
}

static void led(unsigned i, unsigned r, unsigned g, unsigned b) {
    ws2812_set_color(i, (uint8_t)dim(r), (uint8_t)dim(g), (uint8_t)dim(b));
}

/* centre-out level meter */
static void meter(unsigned level /*0..255*/, unsigned r, unsigned g, unsigned b, unsigned base) {
    const unsigned lit = (level * 4u + 127u) / 255u;  /* 0..4 rings from centre */
    for (unsigned i = 0; i < FWOG_LED_COUNT; i++) {
        const unsigned dist = i > 3u ? i - 3u : 3u - i;
        const unsigned on = dist < lit;
        const unsigned k = on ? 255u : base;
        led(i, r * k / 255u, g * k / 255u, b * k / 255u);
    }
}

static void leds_service(uint32_t now) {
    if (!s_leds_ok) return;
    if (s_led_custom) {
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++)
            led(i, s_led_rgb[i][0], s_led_rgb[i][1], s_led_rgb[i][2]);
        ws2812_process();
        return;
    }
    const unsigned tri = (now / 8u) % 512u;
    const unsigned breathe = tri < 256u ? tri : 511u - tri;   /* 0..255..0, ~4 s */
    switch ((wili_mood_t)g_face.shown) {
    case WILI_MOOD_LISTEN:
        /* clearly lit while listening, brighter towards the edges with voice */
        meter(g_face.mic, 40, 200, 190, 70);
        break;
    case WILI_MOOD_THINK: {
        const unsigned head = (now / 90u) % FWOG_LED_COUNT;
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) {
            const unsigned d = (head + FWOG_LED_COUNT - i) % FWOG_LED_COUNT;
            const unsigned k = d == 0 ? 255u : d == 1 ? 90u : d == 2 ? 30u : 4u;
            led(i, 120u * k / 255u, 60u * k / 255u, 220u * k / 255u);
        }
        break;
    }
    case WILI_MOOD_TALK:
        meter(g_face.mouth, 60, 220, 120, 10);
        break;
    case WILI_MOOD_HAPPY:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) {
            const unsigned h = (now / 6u + i * 36u) % 216u;   /* cheap rainbow */
            const unsigned seg = h / 72u, f = (h % 72u) * 255u / 72u;
            if (seg == 0) led(i, (255u - f) / 3u, f / 3u, 0);
            else if (seg == 1) led(i, 0, (255u - f) / 3u, f / 3u);
            else led(i, f / 3u, 0, (255u - f) / 3u);
        }
        break;
    case WILI_MOOD_CONFUSED:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) led(i, breathe / 3u, breathe / 5u, 0);
        break;
    case WILI_MOOD_SAD:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) led(i, 0, 0, 6u + breathe / 12u);
        break;
    case WILI_MOOD_SLEEP:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) led(i, 0, 0, i == 3 ? breathe / 24u : 0);
        break;
    case WILI_MOOD_BORED:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) led(i, 0, 0, 0);
        break;
    case WILI_MOOD_WAKE:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++) led(i, 90, 110, 120);
        break;
    case WILI_MOOD_IDLE:
    default:
        for (unsigned i = 0; i < FWOG_LED_COUNT; i++)
            led(i, 0, 4u + breathe / 20u, 4u + breathe / 22u);
        break;
    }
    ws2812_process();
}

/* =========================================================== commands */

static void send_spk_status(void) {
    const uint32_t used = s_wr - s_rd, free_n = RING_N - used;
    uint8_t st[21];
    memcpy(&st[0], &s_spk_played, 4);
    memcpy(&st[4], &s_spk_received, 4);
    memcpy(&st[8], &s_spk_dropped, 4);
    st[12] = (uint8_t)used; st[13] = (uint8_t)(used >> 8);
    st[14] = (uint8_t)free_n; st[15] = (uint8_t)(free_n >> 8);
    st[16] = (uint8_t)s_underruns; st[17] = (uint8_t)(s_underruns >> 8);
    st[18] = s_spk_active ? 1u : 0u;
    st[19] = s_spk_end ? 1u : 0u;
    st[20] = 0;
    send_frame(WILI_EV_SPK, st, sizeof st, true);
}

static void send_hello(void) {
    static const char hello[] = "WILI 1 " WILI_FW_VERSION;
    send_frame(WILI_EV_HELLO, hello, sizeof hello - 1u, false);
    send_spk_status();   /* gives the host its counter baseline */
}

static void handle(uint8_t type, const uint8_t *p, uint16_t len) {
    switch (type) {
    case WILI_CMD_PING:
        send_hello();
        break;
    case WILI_CMD_MIC:
        s_mic_stream = len >= 1 && p[0] != 0;
        break;
    case WILI_CMD_AUDIO:
        spk_push(p, len);
        break;
    case WILI_CMD_AUDIO_END:
        s_spk_end = true;
        break;
    case WILI_CMD_FLUSH:
        spk_flush();
        break;
    case WILI_CMD_FACE:
        if (len >= 1 && p[0] < WILI_MOOD_COUNT) face_set_mood((wili_mood_t)p[0]);
        break;
    case WILI_CMD_VOLUME:
        if (len >= 1) i2s_audio_set_volume(p[0] > 10 ? 10 : p[0]);
        break;
    case WILI_CMD_CHIME:
        if (len >= 1) spk_chime(p[0]);
        break;
    case WILI_CMD_LEDS:
        s_led_custom = len >= 1 && p[0] == 1 && len >= 1u + 3u * FWOG_LED_COUNT;
        if (s_led_custom) memcpy(s_led_rgb, p + 1, 3u * FWOG_LED_COUNT);
        break;
    case WILI_CMD_LED_LEVEL:
        if (len >= 1) s_led_level = p[0];
        break;
    case WILI_CMD_ROTATE:   /* from 008 the board turns the face itself; still honoured */
        if (len >= 1) g_face.rotation = p[0] & 3u;
        break;
    case WILI_CMD_CAPTION:
        face_set_caption(p, len);
        break;
    default:
        break;
    }
}

/* Byte-wise parser: magic, magic, type, len lo, len hi, payload, crc. */
static uint8_t s_rx[5u + WILI_MAX_PAYLOAD + 1u];
static uint32_t s_rx_n, s_rx_need;

static void rx_byte(uint8_t b) {
    if (s_rx_n == 0) {
        if (b == WILI_MAGIC0) s_rx[s_rx_n++] = b;
        return;
    }
    if (s_rx_n == 1) {
        if (b == WILI_MAGIC1) s_rx[s_rx_n++] = b;
        else s_rx_n = (b == WILI_MAGIC0) ? 1u : 0u;
        return;
    }
    s_rx[s_rx_n++] = b;
    if (s_rx_n == 5) {
        const uint32_t len = (uint32_t)s_rx[3] | ((uint32_t)s_rx[4] << 8);
        if (len > WILI_MAX_PAYLOAD) { s_rx_n = 0; return; }
        s_rx_need = 6u + len;
    }
    if (s_rx_n >= 5 && s_rx_n == s_rx_need) {
        const uint16_t len = (uint16_t)(s_rx_n - 6u);
        if (crc8(&s_rx[2], 3u + len) == s_rx[s_rx_n - 1u]) handle(s_rx[2], &s_rx[5], len);
        s_rx_n = 0;
    }
}

static void usb_rx_service(void) {
    static char buf[512];
    for (int i = 0; i < 16; i++) {
        const int n = stdio_get_until(buf, sizeof buf, get_absolute_time());
        if (n <= 0) break;
        for (int k = 0; k < n; k++) rx_byte((uint8_t)buf[k]);
    }
}

/* =========================================================== sensors */

/* Accelerometer: every new 100 Hz sample is queued and sent in batches; the
   host does the gesture work (taps, face-down, pick-up), so tuning never
   needs a reflash. The board itself only does what has to work without the
   host too (on_board_motion below). lis3dh_process() leaves the sample
   untouched when there is no new data yet, hence the sentinel. */
#define ACCEL_BATCH 16u
static bool s_accel_ok;
static int16_t s_accel_q[ACCEL_BATCH * 3u];
static unsigned s_accel_n;
static uint32_t s_accel_next_poll, s_accel_next_send;

/* On the board, from smoothed gravity (same thresholds as the host's
   motion.py):
   - which way up the screen is, so the face turns to match -- always, host
     or not: 800 mg in the screen's plane, held 0.5 s;
   - face down = asleep, stood back up = awake -- only while no host is
     connected (with one, the host sets the face, and mutes itself):
     750 mg into the desk, held 0.8 s. */
#define ROT_MG        800
#define ROT_HOLD_MS   500u
#define POSE_MG       750
#define POSE_HOLD_MS  800u
static float s_gx, s_gy = -1000.0f, s_gz;
static uint8_t s_rot_cand;
static uint32_t s_rot_since;
static bool s_face_down, s_fd_cand;
static uint32_t s_fd_since;

static void on_board_motion(int16_t x, int16_t y, int16_t z, uint32_t now, bool host) {
    s_gx += 0.1f * ((float)x - s_gx);
    s_gy += 0.1f * ((float)y - s_gy);
    s_gz += 0.1f * ((float)z - s_gz);

    /* standing y = -1 g; on its right side x = +1 g; upside down y = +1 g */
    int rot = -1;
    if (s_gy <= -ROT_MG) rot = 0;
    else if (s_gx >= ROT_MG) rot = 1;
    else if (s_gy >= ROT_MG) rot = 2;
    else if (s_gx <= -ROT_MG) rot = 3;
    if (rot < 0 || (uint8_t)rot != s_rot_cand) {   /* flat, in between, or new */
        s_rot_cand = rot < 0 ? g_face.rotation : (uint8_t)rot;
        s_rot_since = now;
    } else if ((uint8_t)rot != g_face.rotation && now - s_rot_since >= ROT_HOLD_MS) {
        g_face.rotation = (uint8_t)rot;
    }

    const bool fd = s_gz <= -POSE_MG;
    if (fd != s_fd_cand) {
        s_fd_cand = fd;
        s_fd_since = now;
    } else if (fd != s_face_down && now - s_fd_since >= POSE_HOLD_MS) {
        s_face_down = fd;
        if (!host) {
            if (fd) face_set_mood(WILI_MOOD_SLEEP);
            else g_face.poke_seq++;        /* a poke wakes a sleeping face */
        }
    }
}

static void accel_service(uint32_t now, bool host) {
    if (!s_accel_ok) return;
    if ((int32_t)(now - s_accel_next_poll) >= 0) {
        s_accel_next_poll = now + 4u;
        lis3dh_sample_t samp = {0x7FFF, 0x7FFF, 0x7FFF};
        if (lis3dh_process(LIS3DH_MOVE_THRESHOLD_DEFAULT, &samp, NULL) &&
            !(samp.x == 0x7FFF && samp.y == 0x7FFF && samp.z == 0x7FFF) &&
            s_accel_n < ACCEL_BATCH) {
            s_accel_q[s_accel_n * 3u + 0u] = (int16_t)lis3dh_raw_to_mg(samp.x, LIS3DH_RANGE_2G);
            s_accel_q[s_accel_n * 3u + 1u] = (int16_t)lis3dh_raw_to_mg(samp.y, LIS3DH_RANGE_2G);
            s_accel_q[s_accel_n * 3u + 2u] = (int16_t)lis3dh_raw_to_mg(samp.z, LIS3DH_RANGE_2G);
            on_board_motion(s_accel_q[s_accel_n * 3u + 0u], s_accel_q[s_accel_n * 3u + 1u],
                            s_accel_q[s_accel_n * 3u + 2u], now, host);
            if (host) s_accel_n++;
        }
    }
    if (s_accel_n && ((int32_t)(now - s_accel_next_send) >= 0 || s_accel_n == ACCEL_BATCH)) {
        s_accel_next_send = now + 40u;
        send_frame(WILI_EV_ACCEL, s_accel_q, (uint16_t)(s_accel_n * 6u), true);
        s_accel_n = 0;   /* dropped if the host is behind: motion is live data */
    }
}

/* Charger: read only. The BQ25896 charges on its power-on defaults; this
   never resets or reconfigures it (bq25896_read_all() only re-arms the
   ADC's continuous conversion). */
static uint32_t s_power_next;

static void power_service(uint32_t now) {
    if ((int32_t)(now - s_power_next) < 0) return;
    s_power_next = now + 2000u;
    wili_power_t w = {0};
    bq25896_telemetry_t t;
    if (bq25896_read_all(&t)) {
        w.ok = 1;
        w.vbus_stat = (uint8_t)t.vbus_stat;
        w.chg_stat = (uint8_t)t.chg_stat;
        w.vbus_attached = t.vbus_attached ? 1u : 0u;
        w.chg_fault = (uint8_t)t.chg_fault;
        w.ts_rank = (uint8_t)t.ts_rank;
        w.thermal_reg = t.thermal_regulation ? 1u : 0u;
        w.tspct = (uint8_t)(t.tspct_percent + 0.5f);
        w.vbat_mv = t.vbat_mv;
        w.vsys_mv = t.vsys_mv;
        w.vbus_mv = t.vbus_mv;
        w.ichg_ma = t.ichg_ma;
    }
    send_frame(WILI_EV_POWER, &w, sizeof w, true);
}

/* =========================================================== main */

int main(void) {
    board_init();
    /* Binary protocol on the stdio CDC: no \n -> \r\n translation. */
    stdio_set_translate_crlf(&stdio_usb, false);

    multicore_reset_core1();
    multicore_launch_core1(face_core1_main);

    /* pio0: WS2812 sm0, PDM sm1, I2S sm2 -- the BSP's allocation. */
    s_leds_ok = ws2812_init(pio0, 0u);
    s_mic_ok = pdm_mic_init(pio0, 1u);
    fwog_cic_init(&s_cic);
    s_audio_ok = i2s_audio_init(pio0, 2u);
    if (s_audio_ok) i2s_audio_set_volume(9);

    /* I2C1 is up (board_init). Turn on the charger's ADC once so the first
       report already has fresh numbers. */
    lis3dh_init();
    s_accel_ok = lis3dh_configure(LIS3DH_RANGE_2G);
    (void)bq25896_adc_continuous_enable();

    bool was_connected = false;
    uint8_t last_face = 0xFF;
    uint32_t next_led = 0, next_status = 0;

    while (true) {
        const uint32_t now = to_ms_since_boot(get_absolute_time());
        const fwog_power_t power = fwog_power_poll(now);

        usb_rx_service();
        mic_service();
        spk_service(now);
        const bool connected = stdio_usb_connected();
        accel_service(now, connected);
        if (connected) power_service(now);

        if (connected && !was_connected) send_hello();
        if (!connected && was_connected) {
            s_mic_stream = false;      /* host went away */
            spk_flush();
            /* Don't stay stuck thinking or talking: back to being on its own
               (asleep if it's lying face down). */
            face_set_mood(s_face_down ? WILI_MOOD_SLEEP : WILI_MOOD_IDLE);
            face_set_caption(NULL, 0);   /* e.g. a sleep countdown nobody runs now */
        }
        was_connected = connected;

        const fwog_buttons_t *bt = &power.buttons;
        if (bt->pressed || bt->released) {
            const uint8_t ev[3] = {bt->down, bt->pressed, bt->released};
            send_frame(WILI_EV_BUTTON, ev, sizeof ev, false);
            if (bt->pressed) g_face.poke_seq++;
        }

        if (g_face.shown != last_face) {
            last_face = g_face.shown;
            send_frame(WILI_EV_FACE, &last_face, 1, false);
        }

        /* Speaker status every 50 ms while there's audio, and for a second
           after, so the host always sees the final "stopped" state. */
        if (s_spk_active || (s_wr - s_rd) > 0) s_spk_last_active_ms = now;
        if ((int32_t)(now - next_status) >= 0) {
            next_status = now + 50u;
            if (now - s_spk_last_active_ms < 1000u) send_spk_status();
        }

        /* the power countdown owns the LED bar while armed */
        if (!power.armed && (int32_t)(now - next_led) >= 0) {
            next_led = now + 33u;
            leds_service(now);
        }
        sleep_us(200);
    }
}
