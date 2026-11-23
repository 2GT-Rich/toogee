/* Wili USB protocol -- binary frames on the display CPU's USB CDC port.
 *
 * Frame:  0xF7 0x1A | type:u8 | len:u16 LE | payload[len] | crc8
 *         crc8 = CRC-8 (poly 0x07, init 0x00) over type, len (2 bytes), payload.
 *
 * The CDC port is also pico_stdio_usb, so the BSP's DIAG() text can appear
 * between frames; the host parser resyncs on the two magic bytes and the CRC.
 * Opening the port at 1200 baud still reboots into BOOTSEL (contract rule 3).
 *
 * Audio is 8 kHz, 16-bit signed, little-endian, mono, both directions.
 * The Python side lives in wili_board.py (host); keep the two in step.
 */
#ifndef WILI_PROTO_H
#define WILI_PROTO_H

#include <stdint.h>

#define WILI_PROTO_VERSION 1
#define WILI_MAGIC0 0xF7u
#define WILI_MAGIC1 0x1Au
#define WILI_MAX_PAYLOAD 1024u

/* ---- device -> host ---- */
#define WILI_EV_HELLO   0x01u  /* ascii "WILI <proto> <fw version>"          */
#define WILI_EV_MIC     0x02u  /* int16 samples (256 per frame = 32 ms)      */
#define WILI_EV_BUTTON  0x03u  /* u8 down, u8 pressed, u8 released (bit/btn) */
#define WILI_EV_SPK     0x04u  /* u32 played, u32 received, u32 dropped (all
                                  samples since boot), u16 ring used, u16 ring
                                  free, u16 underruns, u8 playing, u8 end
                                  flagged, u8 reserved. Sent every 50 ms while
                                  audio is active and for 1 s after.        */
#define WILI_EV_FACE    0x05u  /* u8 mood now shown (after auto bored/sleep) */
#define WILI_EV_ACCEL   0x06u  /* (firmware 005+) int16 x, y, z in milli-g, per
                                  sample, oldest first: the LIS3DH at 100 Hz,
                                  +-2 g, sent in batches every 40 ms.        */
#define WILI_EV_POWER   0x07u  /* (firmware 005+) wili_power_t, every 2 s     */

/* ---- host -> device ---- */
#define WILI_CMD_PING     0x10u  /* -> HELLO                                  */
#define WILI_CMD_MIC      0x11u  /* u8 1=stream mic, 0=stop                   */
#define WILI_CMD_AUDIO    0x12u  /* int16 samples to play                     */
#define WILI_CMD_AUDIO_END 0x13u /* no more audio this utterance: stop when
                                    the ring drains                          */
#define WILI_CMD_FLUSH    0x14u  /* drop queued audio and stop now            */
#define WILI_CMD_FACE     0x15u  /* u8 mood (wili_mood_t)                     */
#define WILI_CMD_VOLUME   0x16u  /* u8 0..10                                  */
#define WILI_CMD_LEDS     0x17u  /* u8 mode: 0=auto by mood, 1=custom + 7xRGB */
#define WILI_CMD_CHIME    0x18u  /* u8 id: plays a short built-in earcon now     */
#define WILI_CMD_LED_LEVEL 0x19u /* u8 0..255 global LED brightness (default 64) */
#define WILI_CMD_ROTATE   0x1Au  /* (firmware 006+) u8 0..3: turn the face a
                                    quarter turn at a time, clockwise        */
#define WILI_CMD_CAPTION  0x1Bu  /* (firmware 009+) up to 24 ASCII chars shown
                                    along the bottom of the screen, e.g.
                                    "sleeping... 29:41"; empty clears it.
                                    Cleared when the host goes away.         */

#define WILI_CHIME_LISTEN 1u     /* rising blip-blip: "I'm listening"           */
#define WILI_CHIME_DONE   2u     /* falling blip-blip: "got it"                 */
#define WILI_CHIME_ERROR  3u     /* low double buzz                             */

/* Buttons, bit per fwog_btn_id_t: gray=0 yellow=1 green=2 blue=3 red=4. */

/* WILI_EV_POWER payload, little-endian, packed: the BQ25896 charger's view.
   ok=0 means the charger didn't answer (the rest is then zero). */
typedef struct __attribute__((packed)) {
    uint8_t  ok;
    uint8_t  vbus_stat;      /* 0 none, 1 USB host, 2 adapter, 7 OTG         */
    uint8_t  chg_stat;       /* 0 not charging, 1 pre-charge, 2 fast, 3 done  */
    uint8_t  vbus_attached;  /* 1 = USB power present                         */
    uint8_t  chg_fault;      /* 0 ok, 1 input, 2 thermal shutdown, 3 timer    */
    uint8_t  ts_rank;        /* battery thermistor: 0 normal, 2 warm, 3 cool,
                                5 cold, 6 hot                                 */
    uint8_t  thermal_reg;    /* 1 = charger in thermal regulation             */
    uint8_t  tspct;          /* thermistor reading, % of REGN                 */
    uint16_t vbat_mv;
    uint16_t vsys_mv;
    uint16_t vbus_mv;
    uint16_t ichg_ma;        /* actual charge current                         */
} wili_power_t;

#endif
