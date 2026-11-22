/* Wili's face -- core 1.
 *
 * Rendering: each frame builds a small draw list of anti-aliased primitives
 * (rounded rects, arcs, parabola strokes, eyelid half-planes, font glyphs),
 * then rasterises the 320x240 screen in 16-row bands into two ping-pong
 * buffers, DMA-ing one band to the panel while the next is drawn. Bands that
 * held nothing last frame and hold nothing now are skipped, so only the face
 * itself costs SPI time.
 *
 * Geometry is fixed point, 1/16 px (Q4). The RP2040 has no FPU, so per-pixel
 * work is integer only; floats are used per frame or per column.
 *
 * Behaviour: core 0 requests a mood; this file turns it into a pose and adds
 * the life on top -- blinks, glances, breathing, lip sync from the speaker
 * envelope, ears that pulse with the mic, getting bored, falling asleep.
 */
#include "face.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "fwog_display.h"
#include "pico/stdlib.h"

wili_face_t g_face;

/* The screen is 320x240, or 240x320 when the board stands on its side
   (st7789_set_rotation); s_w x s_h is the current one. */
#define SCR_MAX 320
#define BAND_H 16
#define NBANDS_MAX (SCR_MAX / BAND_H)
static int s_w = 320, s_h = 240;
#define Q 16

/* ---- timing (ms) ---- */
#define BORED_AFTER_MS   45000u
#define SLEEP_AFTER_MS  150000u
#define WAKE_MS            420u
#define TRANSIENT_MS      4000u   /* happy / confused / sad then back to idle */
#define GIGGLE_MS         1300u
#define FRAME_MIN_MS        30u
#define TURN_MS           650u    /* the face swinging upright after a turn */
#define DIZZY_MS         1600u    /* then the dizzy face */

/* ---- layout: set by apply_layout() for landscape or portrait ---- */
static float CX = 160.0f;        /* face centre x */
static float EYE_DX = 64.0f;     /* eye centre offset from CX */
static float EYE_Y = 100.0f;
static float MOUTH_Y = 182.0f;
static float XTRA_X = 0.0f;      /* shifts the corner extras (thinking dots, ?, Zzz) */

static uint16_t C_BG, C_EYE, C_HI, C_MOUTH, C_TONGUE, C_BLUSH, C_TEAR, C_TEXT,
                C_DOT, C_EAR;

static uint16_t s_band[2][SCR_MAX * BAND_H];

/* =========================================================== primitives */

typedef enum { P_RRECT, P_RING, P_PARA, P_LID, P_GLYPH } ptype_t;

/* RING halves: which side of the arc to keep. */
enum { HALF_NONE = 0, HALF_UP, HALF_DOWN, HALF_RIGHT, HALF_LEFT };

typedef struct {
    uint8_t  type;
    uint8_t  alpha;            /* 0..255 */
    uint16_t color;
    int16_t  bx0, by0, bx1, by1;   /* pixel bbox on screen, half-open, already clipped */
    int16_t  fx0, fy0, fx1, fy1;   /* the same in face space, before any turn (see below) */
    int32_t  cx, cy;           /* Q4 */
    int32_t  a, b, c;          /* type-specific, see adders */
} prim_t;

#define MAX_PRIMS 64
static prim_t s_prims[MAX_PRIMS];
static int s_nprims;

static inline int32_t q4(float v) { return (int32_t)lroundf(v * (float)Q); }
static inline int imin(int a, int b) { return a < b ? a : b; }
static inline int imax(int a, int b) { return a > b ? a : b; }
static inline float fclampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

typedef struct { int x0, y0, x1, y1; } clip_t;
static clip_t s_clip_screen = {0, 0, 320, 240};
#define CLIP_SCREEN s_clip_screen

/* While the face swings round after the board is turned, everything is
   composed in "face space" as usual and drawn rotated by s_ang about the
   screen centre: screen = C + R(ang)(face - C). s_xf is false the rest of
   the time, and drawing takes the plain, faster path. */
static bool  s_xf;
static float s_cos = 1.0f, s_sin = 0.0f, s_pcx = 160.0f, s_pcy = 120.0f;
static int32_t s_qcos, s_qsin;   /* Q16, per pixel step */

static void set_turn(float ang) {
    s_xf = fabsf(ang) > 0.002f;
    s_cos = cosf(ang); s_sin = sinf(ang);
    s_pcx = (float)s_w * 0.5f; s_pcy = (float)s_h * 0.5f;
    s_qcos = (int32_t)lroundf(s_cos * 65536.0f);
    s_qsin = (int32_t)lroundf(s_sin * 65536.0f);
}

static prim_t *new_prim(ptype_t t, uint16_t color, float alpha, float bx0,
                        float by0, float bx1, float by1, clip_t clip) {
    if (s_nprims >= MAX_PRIMS) return NULL;
    const int a = (int)lroundf(fclampf(alpha, 0.0f, 1.0f) * 255.0f);
    if (a <= 0) return NULL;
    int x0 = imax((int)floorf(bx0) - 1, clip.x0), y0 = imax((int)floorf(by0) - 1, clip.y0);
    int x1 = imin((int)ceilf(bx1) + 1, clip.x1), y1 = imin((int)ceilf(by1) + 1, clip.y1);
    if (x0 >= x1 || y0 >= y1) return NULL;
    int sx0 = x0, sy0 = y0, sx1 = x1, sy1 = y1;
    if (s_xf) {   /* the screen box around the turned face box */
        float lo_x = 1e9f, lo_y = 1e9f, hi_x = -1e9f, hi_y = -1e9f;
        for (int k = 0; k < 4; k++) {
            const float fx = (float)((k & 1) ? x1 : x0) - s_pcx;
            const float fy = (float)((k & 2) ? y1 : y0) - s_pcy;
            const float X = s_pcx + s_cos * fx - s_sin * fy, Y = s_pcy + s_sin * fx + s_cos * fy;
            lo_x = fminf(lo_x, X); hi_x = fmaxf(hi_x, X);
            lo_y = fminf(lo_y, Y); hi_y = fmaxf(hi_y, Y);
        }
        sx0 = imax((int)floorf(lo_x) - 1, 0); sy0 = imax((int)floorf(lo_y) - 1, 0);
        sx1 = imin((int)ceilf(hi_x) + 1, s_w); sy1 = imin((int)ceilf(hi_y) + 1, s_h);
        if (sx0 >= sx1 || sy0 >= sy1) return NULL;
    }
    prim_t *p = &s_prims[s_nprims++];
    p->type = (uint8_t)t;
    p->alpha = (uint8_t)a;
    p->color = color;
    p->bx0 = (int16_t)sx0; p->by0 = (int16_t)sy0;
    p->bx1 = (int16_t)sx1; p->by1 = (int16_t)sy1;
    p->fx0 = (int16_t)x0; p->fy0 = (int16_t)y0;
    p->fx1 = (int16_t)x1; p->fy1 = (int16_t)y1;
    return p;
}

/* Rounded rectangle, centre (cx,cy), half size (hw,hh), corner radius r.
   r = min(hw,hh) gives a stadium / circle. */
static void add_rrect(float cx, float cy, float hw, float hh, float r,
                      uint16_t color, float alpha, clip_t clip) {
    if (hw < 0.5f || hh < 0.5f) return;
    if (r > hw) r = hw;
    if (r > hh) r = hh;
    prim_t *p = new_prim(P_RRECT, color, alpha, cx - hw, cy - hh, cx + hw, cy + hh, clip);
    if (!p) return;
    p->cx = q4(cx); p->cy = q4(cy);
    p->a = q4(hw); p->b = q4(hh); p->c = q4(r);
}

static void add_circle(float cx, float cy, float r, uint16_t color, float alpha,
                       clip_t clip) {
    add_rrect(cx, cy, r, r, r, color, alpha, clip);
}

/* Arc stroke of radius R, thickness th, keeping one half, with round caps. */
static void add_ring(float cx, float cy, float R, float th, int half,
                     uint16_t color, float alpha, clip_t clip) {
    const float e = R + th;
    prim_t *p = new_prim(P_RING, color, alpha, cx - e, cy - e, cx + e, cy + e, clip);
    if (!p) return;
    p->cx = q4(cx); p->cy = q4(cy);
    p->a = q4(R); p->b = q4(th * 0.5f); p->c = half;
}

/* Parabola stroke y = cy + curve*u^2, u in [-1,1] over x in [cx-hw, cx+hw],
   thickness th, round caps. In screen coordinates curve > 0 bends the ends
   DOWN (a frown); compose_mouth() negates its smile value for this. */
static void add_para(float cx, float cy, float hw, float curve, float th,
                     uint16_t color, float alpha, clip_t clip) {
    if (hw < 1.0f) return;
    const float ylo = fminf(cy, cy + curve) - th, yhi = fmaxf(cy, cy + curve) + th;
    prim_t *p = new_prim(P_PARA, color, alpha, cx - hw - th, ylo, cx + hw + th, yhi, clip);
    if (!p) return;
    p->cx = q4(cx); p->cy = q4(cy);
    p->a = q4(hw); p->b = q4(curve); p->c = q4(th * 0.5f);
}

/* Eyelid: cover everything above (dir=+1) or below (dir=-1) the line through
   (x,y) with slope dy/dx, inside `clip`. */
static void add_lid(float x, float y, float slope, int dir, uint16_t color, clip_t clip) {
    prim_t *p = new_prim(P_LID, color, 1.0f, (float)clip.x0, (float)clip.y0,
                         (float)clip.x1, (float)clip.y1, clip);
    if (!p) return;
    p->cx = q4(x); p->cy = q4(y);
    p->a = (int32_t)lroundf(slope * 256.0f); p->b = dir;
}

static void add_glyph(char ch, float x, float y, int scale, uint16_t color,
                      float alpha) {
    if (scale < 1) scale = 1;
    prim_t *p = new_prim(P_GLYPH, color, alpha, x, y, x + LCD_GLYPH_W * scale,
                         y + LCD_GLYPH_H * scale, CLIP_SCREEN);
    if (!p) return;
    p->cx = (int32_t)lroundf(x); p->cy = (int32_t)lroundf(y);
    p->a = (int32_t)(unsigned char)ch; p->b = scale;
}

/* =========================================================== raster */

static inline uint32_t isqrt32(uint32_t n) {
    uint32_t r = 0, b = 1u << 30;
    while (b > n) b >>= 2;
    while (b) {
        if (n >= r + b) { n -= r + b; r = (r >> 1) + b; }
        else r >>= 1;
        b >>= 2;
    }
    return r;
}

/* Signed distance (Q4, negative inside) -> coverage 0..256 over a 1 px edge. */
static inline int cov_of(int32_t d) {
    const int32_t c = 8 - d;
    if (c <= 0) return 0;
    if (c >= 16) return 256;
    return (int)(c << 4);
}

static inline uint16_t blend565(uint16_t d, uint16_t s, int a) {
    if (a >= 256) return s;
    if (a <= 0) return d;
    const int dr = d >> 11, dg = (d >> 5) & 63, db = d & 31;
    const int sr = s >> 11, sg = (s >> 5) & 63, sb = s & 31;
    const int r = dr + (((sr - dr) * a) >> 8);
    const int g = dg + (((sg - dg) * a) >> 8);
    const int b = db + (((sb - db) * a) >> 8);
    return (uint16_t)((r << 11) | (g << 5) | b);
}

static inline void put(uint16_t *px, uint16_t color, int cov, int alpha) {
    if (cov <= 0) return;
    const int a = (alpha >= 255) ? cov : ((cov * (alpha + 1)) >> 8);
    *px = blend565(*px, color, a);
}

/* Signed distance (Q4) from face-space point (X, Y), also Q4, to primitive p;
   the same maths as raster() below, one point at a time, for the turned path.
   Glyphs answer "fully in" (-16) or "out". */
static int32_t prim_dist(const prim_t *p, int32_t X, int32_t Y) {
    switch (p->type) {
    case P_RRECT: {
        const int32_t hwr = p->a - p->c, hhr = p->b - p->c, r = p->c;
        const int32_t qx = abs(X - p->cx) - hwr, qy = abs(Y - p->cy) - hhr;
        if (qx > 0 && qy > 0) return (int32_t)isqrt32((uint32_t)(qx * qx + qy * qy)) - r;
        return (qx > qy ? qx : qy) - r;
    }
    case P_RING: {
        const int32_t R = p->a, t = p->b, half = p->c;
        const int32_t dx = X - p->cx, dy = Y - p->cy;
        const bool out = (half == HALF_UP && dy > 0) || (half == HALF_DOWN && dy < 0) ||
                         (half == HALF_RIGHT && dx < 0) || (half == HALF_LEFT && dx > 0);
        if (!out) return abs((int32_t)isqrt32((uint32_t)(dx * dx + dy * dy)) - R) - t;
        const int32_t along = (half <= HALF_DOWN) ? abs(dx) : abs(dy);
        const int32_t across = (half <= HALF_DOWN) ? abs(dy) : abs(dx);
        const int32_t e = along - R;
        return (int32_t)isqrt32((uint32_t)(e * e + across * across)) - t;
    }
    case P_PARA: {
        const int32_t hw = p->a, curve = p->b, t = p->c;
        const int32_t ux = X - p->cx;
        if (abs(ux) > hw) {
            const int32_t dx = X - (p->cx + (ux < 0 ? -hw : hw)), dy = Y - (p->cy + curve);
            return (int32_t)isqrt32((uint32_t)(dx * dx + dy * dy)) - t;
        }
        const float u = (float)ux / (float)hw;
        const float slope = 2.0f * (float)curve * u / (float)hw;
        const int32_t fy = p->cy + (int32_t)((float)curve * u * u);
        const int32_t k = (int32_t)(256.0f / sqrtf(1.0f + slope * slope));
        return ((abs(Y - fy) * k) >> 8) - t;
    }
    case P_LID: {
        const int32_t yl = p->cy + (((X - p->cx) * p->a) >> 8);
        return (p->b > 0) ? (Y - yl) : (yl - Y);
    }
    case P_GLYPH: {
        const int px = X >> 4, py = Y >> 4, sc = p->b;
        if (px < p->cx || py < p->cy) return 1000;
        const int col = (px - p->cx) / sc, row = (py - p->cy) / sc;
        if (col >= (int)LCD_GLYPH_W || row >= (int)LCD_GLYPH_H) return 1000;
        return (lcd_font_glyph((char)p->a)[col] & (1u << row)) ? -16 : 1000;
    }
    default:
        return 1000;
    }
}

static void raster_turned(const prim_t *p, uint16_t *buf, int y0, int y1) {
    const int ys = imax(p->by0, y0), ye = imin(p->by1, y1);
    const int xs = p->bx0, xe = p->bx1;
    const int32_t cx0 = p->fx0 * Q, cy0 = p->fy0 * Q, cx1 = p->fx1 * Q, cy1 = p->fy1 * Q;
    for (int y = ys; y < ye; y++) {
        uint16_t *row = buf + (y - y0) * s_w;
        /* face space of this row's first pixel centre: F = C + R(-ang)(S - C), Q16 */
        const float dx = (float)xs + 0.5f - s_pcx, dy = (float)y + 0.5f - s_pcy;
        int32_t fx = (int32_t)lroundf((s_pcx + s_cos * dx + s_sin * dy) * 65536.0f);
        int32_t fy = (int32_t)lroundf((s_pcy - s_sin * dx + s_cos * dy) * 65536.0f);
        for (int x = xs; x < xe; x++, fx += s_qcos, fy -= s_qsin) {
            const int32_t X = fx >> 12, Y = fy >> 12;   /* Q16 -> Q4 */
            if (X < cx0 || X >= cx1 || Y < cy0 || Y >= cy1) continue;
            put(&row[x], p->color, cov_of(prim_dist(p, X, Y)), p->alpha);
        }
    }
}

static void raster(const prim_t *p, uint16_t *buf, int y0, int y1) {
    if (s_xf) { raster_turned(p, buf, y0, y1); return; }
    const int ys = imax(p->by0, y0), ye = imin(p->by1, y1);
    const int xs = p->bx0, xe = p->bx1;
    if (ys >= ye) return;

    switch (p->type) {
    case P_RRECT: {
        const int32_t hwr = p->a - p->c, hhr = p->b - p->c, r = p->c;
        for (int y = ys; y < ye; y++) {
            uint16_t *row = buf + (y - y0) * s_w;
            const int32_t qy = abs(y * Q + 8 - p->cy) - hhr;
            for (int x = xs; x < xe; x++) {
                const int32_t qx = abs(x * Q + 8 - p->cx) - hwr;
                int32_t d;
                if (qx > 0 && qy > 0) d = (int32_t)isqrt32((uint32_t)(qx * qx + qy * qy)) - r;
                else d = (qx > qy ? qx : qy) - r;
                put(&row[x], p->color, cov_of(d), p->alpha);
            }
        }
        break;
    }
    case P_RING: {
        const int32_t R = p->a, t = p->b, half = p->c;
        for (int y = ys; y < ye; y++) {
            uint16_t *row = buf + (y - y0) * s_w;
            const int32_t dy = y * Q + 8 - p->cy;
            for (int x = xs; x < xe; x++) {
                const int32_t dx = x * Q + 8 - p->cx;
                const bool out = (half == HALF_UP && dy > 0) || (half == HALF_DOWN && dy < 0) ||
                                 (half == HALF_RIGHT && dx < 0) || (half == HALF_LEFT && dx > 0);
                int32_t d;
                if (!out) {
                    d = abs((int32_t)isqrt32((uint32_t)(dx * dx + dy * dy)) - R) - t;
                } else {
                    const int32_t along = (half <= HALF_DOWN) ? abs(dx) : abs(dy);
                    const int32_t across = (half <= HALF_DOWN) ? abs(dy) : abs(dx);
                    const int32_t e = along - R;
                    d = (int32_t)isqrt32((uint32_t)(e * e + across * across)) - t;
                }
                put(&row[x], p->color, cov_of(d), p->alpha);
            }
        }
        break;
    }
    case P_PARA: {
        const int32_t hw = p->a, curve = p->b, t = p->c;
        for (int x = xs; x < xe; x++) {
            const int32_t ux = x * Q + 8 - p->cx;
            int32_t fy = 0, k = 256;
            const bool cap = abs(ux) > hw;
            if (!cap) {
                const float u = (float)ux / (float)hw;
                const float slope = 2.0f * (float)curve * u / (float)hw;
                fy = p->cy + (int32_t)((float)curve * u * u);
                k = (int32_t)(256.0f / sqrtf(1.0f + slope * slope));
            }
            const int32_t ex = p->cx + (ux < 0 ? -hw : hw), ey = p->cy + curve;
            for (int y = ys; y < ye; y++) {
                const int32_t Y = y * Q + 8;
                int32_t d;
                if (!cap) {
                    d = ((abs(Y - fy) * k) >> 8) - t;
                } else {
                    const int32_t dx = x * Q + 8 - ex, dy = Y - ey;
                    d = (int32_t)isqrt32((uint32_t)(dx * dx + dy * dy)) - t;
                }
                put(&buf[(y - y0) * s_w + x], p->color, cov_of(d), p->alpha);
            }
        }
        break;
    }
    case P_LID: {
        const int32_t slope = p->a, dir = p->b;
        for (int x = xs; x < xe; x++) {
            const int32_t yl = p->cy + (((x * Q + 8 - p->cx) * slope) >> 8);
            for (int y = ys; y < ye; y++) {
                const int32_t Y = y * Q + 8;
                const int32_t d = (dir > 0) ? (Y - yl) : (yl - Y);
                put(&buf[(y - y0) * s_w + x], p->color, cov_of(d), p->alpha);
            }
        }
        break;
    }
    case P_GLYPH: {
        const uint8_t *g = lcd_font_glyph((char)p->a);
        const int sc = p->b;
        for (int y = ys; y < ye; y++) {
            const int row = (y - p->cy) / sc;
            if (row < 0 || row >= (int)LCD_GLYPH_H) continue;
            for (int x = xs; x < xe; x++) {
                const int col = (x - p->cx) / sc;
                if (col < 0 || col >= (int)LCD_GLYPH_W) continue;
                if (g[col] & (1u << row))
                    put(&buf[(y - y0) * s_w + x], p->color, 256, p->alpha);
            }
        }
        break;
    }
    }
}

static bool s_band_was_used[NBANDS_MAX];
static int s_cur_buf;

static void draw_frame(bool force_all) {
    for (int b = 0; b < s_h / BAND_H; b++) {
        const int y0 = b * BAND_H, y1 = y0 + BAND_H;
        bool used = false;
        for (int i = 0; i < s_nprims && !used; i++)
            used = s_prims[i].by0 < y1 && s_prims[i].by1 > y0;
        if (!used && !s_band_was_used[b] && !force_all) continue;
        s_band_was_used[b] = used;

        uint16_t *buf = s_band[s_cur_buf];
        /* The other buffer may still be on the wire; this one finished before
           the blit that used the other started (blit waits first). */
        const uint32_t fill = ((uint32_t)C_BG << 16) | C_BG;
        uint32_t *w = (uint32_t *)buf;
        for (int i = 0; i < s_w * BAND_H / 2; i++) w[i] = fill;
        if (used)
            for (int i = 0; i < s_nprims; i++) raster(&s_prims[i], buf, y0, y1);
        st7789_blit_rect_async(0, (uint16_t)y0, (uint16_t)s_w, BAND_H, buf);
        s_cur_buf ^= 1;
    }
}

/* =========================================================== behaviour */

static uint32_t s_rng = 0x9E3779B9u;
static uint32_t rnd(void) {
    s_rng ^= s_rng << 13; s_rng ^= s_rng >> 17; s_rng ^= s_rng << 5;
    return s_rng;
}
static float frand(float lo, float hi) { return lo + (hi - lo) * (float)(rnd() & 0xFFFF) / 65535.0f; }

typedef struct {
    float gx, gy;        /* gaze offset, px */
    float ew, eh;        /* eye half size */
    float lid;           /* top lid 0..1 of eye height */
    float tilt;          /* + = sad (outer corners low), - = cross */
    float squint;        /* bottom curved lid 0..1 */
    float happy;         /* 0..1, >0.5 draws ^ ^ */
    float asleep;        /* 0..1, >0.5 draws closed arcs */
    float lopsided;      /* confused: left eye smaller, right bigger */
    float mw;            /* mouth half width */
    float curve;         /* mouth curve px, + smile */
    float mopen;         /* baseline mouth opening px */
    float mx;            /* mouth x offset */
    float blush;         /* 0..1 */
    float bob;           /* face y offset */
    float glow;          /* backlight 0..255 */
} pose_t;

static pose_t P;          /* current */
static pose_t T;          /* target */

static wili_mood_t s_mood = WILI_MOOD_IDLE;
static wili_mood_t s_after_wake = WILI_MOOD_IDLE;
static uint32_t s_mood_ms, s_activity_ms;
static uint32_t s_seen_seq, s_seen_poke;
static bool     s_giggle;                 /* poke-triggered happy */

static uint32_t s_next_blink, s_blink_start;
static bool     s_double_blink;
static uint32_t s_next_glance;
static float    s_glance_x, s_glance_y;
static uint32_t s_next_yawn, s_yawn_start;
static uint32_t s_peek_until;
static float    s_mouth_env, s_mic_env;
static uint32_t s_turn_ms;                /* when the last turn started */
static float    s_turn_from;              /* the face's angle then (rad), easing to 0 */
static bool     s_turn_funny;             /* whoa while it swings, then dizzy */
static bool     s_dizzy;                  /* this frame: spiral eyes */
static uint32_t s_now;

static void enter(wili_mood_t m, uint32_t now) {
    if (m >= WILI_MOOD_COUNT) m = WILI_MOOD_IDLE;
    s_mood = m;
    s_mood_ms = now;
    s_giggle = false;
    if (m == WILI_MOOD_BORED) s_next_yawn = now + 3000u + (rnd() % 4000u);
    g_face.shown = (uint8_t)m;
}

static void update_mood(uint32_t now) {
    const uint32_t seq = g_face.mood_seq;
    if (seq != s_seen_seq) {
        s_seen_seq = seq;
        const wili_mood_t req = (wili_mood_t)g_face.mood;
        s_activity_ms = now;
        const bool dozy = s_mood == WILI_MOOD_SLEEP || s_mood == WILI_MOOD_BORED;
        if (dozy && req != WILI_MOOD_SLEEP && req != WILI_MOOD_BORED && req != WILI_MOOD_IDLE) {
            s_after_wake = req;
            enter(WILI_MOOD_WAKE, now);
        } else if (req == WILI_MOOD_WAKE) {
            s_after_wake = WILI_MOOD_LISTEN;
            enter(WILI_MOOD_WAKE, now);
        } else if (!(dozy && req == WILI_MOOD_IDLE)) {
            enter(req, now);
        }
    }
    const uint32_t poke = g_face.poke_seq;
    if (poke != s_seen_poke) {
        s_seen_poke = poke;
        s_activity_ms = now;
        if (s_mood == WILI_MOOD_SLEEP || s_mood == WILI_MOOD_BORED) {
            s_after_wake = WILI_MOOD_IDLE;
            enter(WILI_MOOD_WAKE, now);
        } else if (s_mood == WILI_MOOD_IDLE || s_mood == WILI_MOOD_HAPPY) {
            enter(WILI_MOOD_HAPPY, now);
            s_giggle = true;
        }
    }

    const uint32_t in_mood = now - s_mood_ms;
    switch (s_mood) {
    case WILI_MOOD_WAKE:
        if (in_mood > WAKE_MS) enter(s_after_wake, now);
        break;
    case WILI_MOOD_HAPPY:
        if (in_mood > (s_giggle ? GIGGLE_MS : TRANSIENT_MS)) enter(WILI_MOOD_IDLE, now);
        break;
    case WILI_MOOD_CONFUSED:
    case WILI_MOOD_SAD:
        if (in_mood > TRANSIENT_MS) enter(WILI_MOOD_IDLE, now);
        break;
    case WILI_MOOD_IDLE:
        if (now - s_activity_ms > BORED_AFTER_MS) enter(WILI_MOOD_BORED, now);
        break;
    case WILI_MOOD_BORED:
        if (now - s_activity_ms > SLEEP_AFTER_MS) enter(WILI_MOOD_SLEEP, now);
        break;
    default:
        break;
    }
}

/* Mood -> target pose, plus the per-mood motion (gaze wander, yawns...). */
static void target_pose(uint32_t now, float t) {
    pose_t n = {
        .gx = 0, .gy = 0, .ew = 36, .eh = 44, .lid = 0, .tilt = 0, .squint = 0,
        .happy = 0, .asleep = 0, .lopsided = 0, .mw = 22, .curve = 5, .mopen = 0,
        .mx = 0, .blush = 0.15f, .bob = 2.5f * sinf(t * 1.8f), .glow = 255,
    };

    /* glances: idle-ish moods look around on their own */
    bool wander = false;
    float rx = 18, ry = 9;
    uint32_t gap_lo = 1200, gap_hi = 4000;

    switch (s_mood) {
    case WILI_MOOD_IDLE:
        wander = true;
        break;
    case WILI_MOOD_LISTEN: {
        const float m = s_mic_env;
        n.ew = 39 + m * 5; n.eh = 49 + m * 6;
        n.gy = -3; n.curve = 3; n.mw = 15; n.blush = 0.1f;
        wander = true; rx = 4; ry = 2; gap_lo = 600; gap_hi = 1500;
        break;
    }
    case WILI_MOOD_THINK: {
        const float side = (sinf(t * 0.7f) > 0) ? 1.0f : -0.6f;
        n.gx = 20 * side; n.gy = -15 + 2 * sinf(t * 2.3f);
        n.lid = 0.18f; n.tilt = -3; n.curve = -1.5f; n.mw = 12; n.mx = 14 * side;
        n.blush = 0;
        break;
    }
    case WILI_MOOD_TALK:
        n.squint = 0.18f; n.curve = 6; n.mw = 22; n.blush = 0.35f;
        n.bob += s_mouth_env * 3.0f;
        wander = true; rx = 6; ry = 3; gap_lo = 900; gap_hi = 2200;
        break;
    case WILI_MOOD_HAPPY:
        n.happy = 1; n.blush = 0.9f; n.curve = 13; n.mw = 33; n.mopen = 0;
        n.bob = -4.0f * fabsf(sinf(t * (s_giggle ? 11.0f : 5.0f)));
        if (s_giggle) n.mopen = 7;
        break;
    case WILI_MOOD_CONFUSED:
        n.lopsided = 1; n.tilt = -5; n.curve = -3; n.mw = 15; n.mx = 12;
        n.gx = 8; n.gy = -6; n.blush = 0;
        break;
    case WILI_MOOD_SAD:
        n.tilt = 12; n.lid = 0.22f; n.curve = -9; n.mw = 20; n.gy = 6;
        n.blush = 0; n.bob = 1.5f * sinf(t * 1.1f) + 3;
        break;
    case WILI_MOOD_BORED: {
        n.lid = 0.48f; n.curve = -0.5f; n.mw = 16; n.mx = 9; n.blush = 0;
        n.gx = 22 * sinf(t * 0.45f); n.gy = 5; n.glow = 170;
        if (now >= s_next_yawn && s_yawn_start == 0) s_yawn_start = now;
        if (s_yawn_start) {
            const float y = (float)(now - s_yawn_start) / 2400.0f;  /* 0..1 */
            if (y >= 1.0f) {
                s_yawn_start = 0;
                s_next_yawn = now + 8000u + (rnd() % 9000u);
            } else {
                const float k = sinf(fminf(y, 1.0f) * 3.14159265f);  /* 0..1..0 */
                n.mopen = 20 * k; n.mw = 16 + 2 * k; n.mx = 0; n.curve = 0;
                n.lid = 0.48f + 0.3f * k; n.squint = 0.45f * k; n.gy = -8 * k;
            }
        }
        break;
    }
    case WILI_MOOD_SLEEP:
        n.asleep = 1; n.curve = 0; n.mw = 7; n.mopen = 2.5f + 1.5f * sinf(t * 1.3f);
        n.bob = 4.0f * sinf(t * 1.3f) + 6; n.glow = 45; n.blush = 0.25f;
        if (s_mic_env > 0.55f && now > s_peek_until + 4000u) s_peek_until = now + 1500u;
        break;
    case WILI_MOOD_WAKE: {
        const float k = 1.0f - (float)(now - s_mood_ms) / (float)WAKE_MS;  /* 1..0 */
        n.ew = 44 + 6 * k; n.eh = 55 + 8 * k; n.curve = 0; n.mw = 9;
        n.mopen = 9 * k + 2; n.gy = -6; n.blush = 0;
        break;
    }
    default:
        break;
    }

    s_dizzy = false;
    if (s_turn_funny) {
        const uint32_t e = now - s_turn_ms;
        if (e < TURN_MS) {
            /* whoa: wide eyes and a little "o" while it swings round */
            n = (pose_t){.ew = 45, .eh = 57, .mw = 9, .mopen = 8, .curve = 0, .gy = -4,
                         .blush = 0, .glow = 255};
            wander = false;
        } else if (e < TURN_MS + DIZZY_MS) {
            /* dizzy: spiral eyes, a wobbly mouth, the head swaying */
            s_dizzy = true;
            n = (pose_t){.ew = 38, .eh = 38, .mw = 18, .curve = 7.0f * sinf(t * 8.0f),
                         .mx = 6.0f * sinf(t * 3.1f), .gx = 9.0f * sinf(t * 6.5f),
                         .gy = 3.0f * sinf(t * 4.1f), .bob = 3.0f * sinf(t * 5.0f),
                         .blush = 0.5f, .glow = 255};
            wander = false;
        } else {
            s_turn_funny = false;
        }
    }

    if (wander) {
        if (now >= s_next_glance) {
            s_next_glance = now + gap_lo + (rnd() % (gap_hi - gap_lo));
            const bool centre = (rnd() % 3u) == 0u;
            s_glance_x = centre ? 0 : frand(-rx, rx);
            s_glance_y = centre ? 0 : frand(-ry, ry);
        }
        n.gx += s_glance_x;
        n.gy += s_glance_y;
    }
    T = n;
}

static float approach(float cur, float tgt, float rate, float dt) {
    const float k = 1.0f - expf(-rate * dt);
    return cur + (tgt - cur) * k;
}

static void ease_pose(float dt) {
    P.gx = approach(P.gx, T.gx, 16, dt);
    P.gy = approach(P.gy, T.gy, 16, dt);
    P.ew = approach(P.ew, T.ew, 12, dt);
    P.eh = approach(P.eh, T.eh, 12, dt);
    P.lid = approach(P.lid, T.lid, 8, dt);
    P.tilt = approach(P.tilt, T.tilt, 8, dt);
    P.squint = approach(P.squint, T.squint, 9, dt);
    P.happy = approach(P.happy, T.happy, 14, dt);
    P.asleep = approach(P.asleep, T.asleep, 6, dt);
    P.lopsided = approach(P.lopsided, T.lopsided, 8, dt);
    P.mw = approach(P.mw, T.mw, 10, dt);
    P.curve = approach(P.curve, T.curve, 9, dt);
    P.mopen = approach(P.mopen, T.mopen, 14, dt);
    P.mx = approach(P.mx, T.mx, 7, dt);
    P.blush = approach(P.blush, T.blush, 4, dt);
    P.bob = approach(P.bob, T.bob, 10, dt);
    P.glow = approach(P.glow, T.glow, 2.5f, dt);
}

/* Blink envelope: 1 = open, dips to ~0.05. */
static float blink_open(uint32_t now) {
    const bool can = s_mood != WILI_MOOD_SLEEP && s_mood != WILI_MOOD_HAPPY &&
                     s_mood != WILI_MOOD_WAKE && !s_turn_funny;
    if (!can) { s_blink_start = 0; return 1.0f; }
    if (!s_blink_start && now >= s_next_blink) {
        s_blink_start = now;
        s_double_blink = (rnd() % 6u) == 0u;
    }
    if (s_blink_start) {
        const uint32_t dur = s_double_blink ? 380u : 150u;
        const uint32_t e = now - s_blink_start;
        if (e >= dur) {
            s_blink_start = 0;
            const uint32_t lo = s_mood == WILI_MOOD_BORED ? 1500u : 2200u;
            s_next_blink = now + lo + (rnd() % 4200u);
            return 1.0f;
        }
        float ph = s_double_blink ? fmodf((float)e / 190.0f, 1.0f) : (float)e / 150.0f;
        const float tri = 1.0f - fabsf(2.0f * ph - 1.0f);   /* 0..1..0 */
        return 1.0f - 0.95f * tri;
    }
    return 1.0f;
}

/* =========================================================== compose */

/* A spinning spiral, made of half-circles: odd ones centred on (cx, cy),
   even ones one step aside, each a step bigger, so their ends join up. The
   four phases turn it a quarter at a time. */
static void compose_spiral(float cx, float cy, float size) {
    static const uint8_t ha[4] = {HALF_UP, HALF_RIGHT, HALF_DOWN, HALF_LEFT};
    static const uint8_t hb[4] = {HALF_DOWN, HALF_LEFT, HALF_UP, HALF_RIGHT};
    static const int8_t ox[4] = {-1, 0, 1, 0}, oy[4] = {0, -1, 0, 1};
    const int ph = (int)((s_now / 70u) & 3u);
    const float d = size / 6.0f;
    for (int k = 1; k <= 5; k++) {
        const bool odd = (k & 1) != 0;
        add_ring(odd ? cx : cx + (float)ox[ph] * d, odd ? cy : cy + (float)oy[ph] * d,
                 d * (float)k, 5.0f, odd ? ha[ph] : hb[ph], C_EYE, 1.0f, CLIP_SCREEN);
    }
}

static void compose_eye(float cx, float cy, float ew, float eh, float open,
                        float sideways /* -1 left eye, +1 right eye */, bool peek) {
    if (s_dizzy) {
        compose_spiral(cx, cy, ew);
        return;
    }
    const float bob_y = cy;
    const clip_t box = {
        (int)(cx - ew - 4), (int)(bob_y - eh - 6), (int)(cx + ew + 5), (int)(bob_y + eh + 6)
    };

    if (P.asleep > 0.5f && !peek) {
        /* closed, content: a downward arc */
        add_ring(cx, bob_y - 2, ew * 0.78f, 7.0f, HALF_DOWN, C_EYE, 1.0f, CLIP_SCREEN);
        return;
    }
    if (P.happy > 0.5f) {
        add_ring(cx, bob_y + eh * 0.28f, ew * 0.82f, 9.0f, HALF_UP, C_EYE, 1.0f, CLIP_SCREEN);
        return;
    }

    float h = eh * open;
    if (peek) h = eh * 0.30f;
    if (h < 3.0f) h = 3.0f;
    const float r = fminf(ew, h) * 0.92f;
    add_rrect(cx, bob_y, ew, h, r, C_EYE, 1.0f, CLIP_SCREEN);

    /* sparkle: big highlight up and to the outside, little one below it */
    if (h > 14.0f) {
        const float hx = cx + ew * 0.34f + P.gx * 0.12f;
        const float hy = bob_y - h * 0.42f + P.gy * 0.10f;
        add_circle(hx, hy, 6.5f, C_HI, 0.95f, box);
        add_circle(hx - 9.0f, hy + 9.0f, 2.8f, C_HI, 0.8f, box);
    }

    /* top lid: tilt makes the outer corner low (sad) or the inner one (cross) */
    const float lid_y = bob_y - h + P.lid * 2.0f * h;
    if (P.lid > 0.01f || fabsf(P.tilt) > 0.5f) {
        /* Lid covers above the line. Sad (tilt > 0) = line lower at the outer
           corner: left eye's outer side is smaller x (negative slope), right
           eye's is larger x (positive slope). */
        const float slope = sideways * P.tilt / (2.0f * ew);
        add_lid(cx, lid_y + fabsf(P.tilt) * 0.5f, slope, +1, C_BG, box);
    }
    /* bottom lid: a big background circle rising from below = smiling eyes */
    if (P.squint > 0.02f) {
        const float R = ew * 1.7f;
        add_circle(cx, bob_y + h + R - P.squint * h * 1.15f, R, C_BG, 1.0f, box);
    }
}

static void compose_mouth(float cx, float cy, float open) {
    if (open > 2.0f) {
        const float hw = P.mw, hh = open + 3.0f;
        add_rrect(cx, cy, hw, hh, fminf(hw, hh), C_EYE, 1.0f, CLIP_SCREEN);
        const float iw = hw - 3.5f, ih = hh - 3.5f;
        if (iw > 1 && ih > 1) {
            add_rrect(cx, cy, iw, ih, fminf(iw, ih), C_MOUTH, 1.0f, CLIP_SCREEN);
            if (ih > 9.0f) {
                const float tw = iw * 0.55f, th = ih * 0.42f;
                add_rrect(cx, cy + ih - th - 1.0f, tw, th, fminf(tw, th), C_TONGUE, 1.0f,
                          CLIP_SCREEN);
            }
        }
    } else {
        /* Screen y grows downward: a smile has its ends *higher*, so the
           parabola's curve is the negative of P.curve. Centre it so the
           mouth's middle stays near MOUTH_Y whatever the curve. */
        add_para(cx, cy + P.curve * 0.5f, P.mw, -P.curve, 6.0f, C_EYE, 1.0f, CLIP_SCREEN);
    }
}

static void compose(uint32_t now, float t, float open_blink) {
    s_nprims = 0;
    s_now = now;
    const float cy = EYE_Y + P.bob + P.gy;
    const float lx = CX - EYE_DX + P.gx, rx = CX + EYE_DX + P.gx;

    float lw = P.ew, lh = P.eh, rw = P.ew, rh = P.eh;
    if (P.lopsided > 0.01f) {
        lw *= 1.0f - 0.18f * P.lopsided; lh *= 1.0f - 0.30f * P.lopsided;
        rw *= 1.0f + 0.06f * P.lopsided; rh *= 1.0f + 0.10f * P.lopsided;
    }
    const bool peek = now < s_peek_until;
    compose_eye(lx, cy + (lh < P.eh ? 4.0f * P.lopsided : 0), lw, lh, open_blink, -1, false);
    compose_eye(rx, cy, rw, rh, open_blink, +1, peek);

    /* blush */
    if (P.blush > 0.03f) {
        const float by = EYE_Y + P.bob + P.eh + 16.0f;
        add_rrect(CX - EYE_DX - 14.0f, by, 18, 8, 8, C_BLUSH, P.blush * 0.55f, CLIP_SCREEN);
        add_rrect(CX + EYE_DX + 14.0f, by, 18, 8, 8, C_BLUSH, P.blush * 0.55f, CLIP_SCREEN);
    }

    /* mouth: baseline opening plus lip sync */
    float open = P.mopen;
    if (s_mood == WILI_MOOD_TALK) open += 2.0f + s_mouth_env * 20.0f;
    compose_mouth(CX + P.mx + P.gx * 0.25f, MOUTH_Y + P.bob * 0.8f + P.gy * 0.2f, open);

    /* --- extras --- */
    if (s_mood == WILI_MOOD_LISTEN) {
        /* Sound waves on both sides: always clearly visible while listening
           (so there's no doubt it's listening), brighter and wider with voice,
           with a slow ripple when it's quiet. */
        const float m = s_mic_env;
        for (int i = 0; i < 3; i++) {
            const float ripple = 0.5f + 0.5f * sinf(t * 5.0f - (float)i * 1.2f);
            const float lvl = fclampf(0.45f + 0.25f * ripple + m * 3.0f - (float)i * 0.5f, 0.45f, 1.0f);
            const float R = 12.0f + (10.0f + m * 4.0f) * (float)i;
            add_ring(rx + P.ew + 4.0f, cy, R, 5.0f, HALF_RIGHT, C_EAR, lvl, CLIP_SCREEN);
            add_ring(lx - P.ew - 4.0f, cy, R, 5.0f, HALF_LEFT, C_EAR, lvl, CLIP_SCREEN);
        }
    } else if (s_mood == WILI_MOOD_THINK) {
        for (int i = 0; i < 3; i++) {
            const float ph = t * 7.0f - (float)i * 1.1f;
            const float up = fmaxf(0.0f, sinf(ph)) * 7.0f;
            add_circle(XTRA_X + 236.0f + 17.0f * (float)i, 34.0f - up, 5.5f, C_DOT, 1.0f, CLIP_SCREEN);
        }
    } else if (s_mood == WILI_MOOD_CONFUSED) {
        add_glyph('?', XTRA_X + 246.0f, 12.0f + 3.0f * sinf(t * 3.0f), 5, C_TEXT, 1.0f);
    } else if (s_mood == WILI_MOOD_SAD) {
        const float ph = fmodf(t * 0.45f, 1.0f);
        const float ty = EYE_Y + P.eh + 4.0f + ph * 46.0f;
        const float fade = ph < 0.15f ? ph / 0.15f : (1.0f - ph) / 0.85f;
        add_rrect(lx - 16.0f, ty, 4.5f, 6.5f, 4.5f, C_TEAR, fade, CLIP_SCREEN);
    } else if (s_mood == WILI_MOOD_SLEEP) {
        for (int i = 0; i < 3; i++) {
            const float ph = fmodf(t * 0.33f + (float)i / 3.0f, 1.0f);
            const float x = XTRA_X + 232.0f + ph * 46.0f + 5.0f * sinf(ph * 9.0f);
            const float y = 74.0f - ph * 66.0f;
            const int sc = 2 + (int)(ph * 3.0f);
            const float a = ph < 0.2f ? ph / 0.2f : (ph > 0.7f ? (1.0f - ph) / 0.3f : 1.0f);
            add_glyph(i == 1 ? 'Z' : 'z', x, y, sc, C_TEXT, a);
        }
    }
}

/* =========================================================== rotation */

/* The face's angle now: s_turn_from easing to 0 with a little overshoot, so
   it swings past upright and settles back like it's on a spring. */
static float turn_angle(uint32_t now) {
    const uint32_t e = now - s_turn_ms;
    if (s_turn_from == 0.0f || e >= TURN_MS) return 0.0f;
    const float u = (float)e / (float)TURN_MS - 1.0f;            /* -1..0 */
    const float back = 1.0f + 2.70158f * u * u * u + 1.70158f * u * u;   /* ease-out-back */
    return s_turn_from * (1.0f - back);
}

/* Landscape keeps the original layout. Portrait (240 wide, 320 tall) brings
   the eyes closer and centres the face lower; the corner extras move in to
   stay on screen. */
static void apply_layout(void) {
    s_w = st7789_width();
    s_h = st7789_height();
    s_clip_screen = (clip_t){0, 0, s_w, s_h};
    CX = (float)s_w * 0.5f;
    if (s_w >= s_h) {
        EYE_DX = 64.0f; EYE_Y = 100.0f; MOUTH_Y = 182.0f; XTRA_X = 0.0f;
    } else {
        EYE_DX = 52.0f; EYE_Y = 140.0f; MOUTH_Y = 222.0f; XTRA_X = (float)s_w - 320.0f;
    }
    memset(s_band_was_used, 0, sizeof s_band_was_used);
}

/* =========================================================== main loop */

static void init_colors(void) {
    C_BG     = st7789_rgb565(10, 14, 34);
    C_EYE    = st7789_rgb565(110, 240, 225);
    C_HI     = st7789_rgb565(255, 255, 255);
    C_MOUTH  = st7789_rgb565(36, 18, 46);
    C_TONGUE = st7789_rgb565(255, 110, 150);
    C_BLUSH  = st7789_rgb565(255, 105, 160);
    C_TEAR   = st7789_rgb565(120, 180, 255);
    C_TEXT   = st7789_rgb565(170, 200, 255);
    C_DOT    = st7789_rgb565(200, 170, 255);
    C_EAR    = st7789_rgb565(110, 240, 225);
}

void face_core1_main(void) {
    init_colors();
    st7789_init_begin();
    const absolute_time_t deadline = make_timeout_time_ms(600);
    while (!st7789_ready() && !time_reached(deadline)) {
        st7789_init_step();
        sleep_ms(1);
    }
    if (!st7789_ready()) {
        g_face.lcd_ok = 0;
        while (true) sleep_ms(1000);
    }
    st7789_clear(C_BG);
    st7789_dma_wait();
    board_backlight(255);
    g_face.lcd_ok = 1;

    uint32_t now = to_ms_since_boot(get_absolute_time());
    s_rng ^= time_us_32();
    s_activity_ms = s_mood_ms = now;
    s_next_blink = now + 1500u;
    s_next_glance = now + 800u;
    P = (pose_t){.ew = 36, .eh = 44, .mw = 22, .curve = 5, .glow = 255};
    enter(WILI_MOOD_IDLE, now);
    /* Wake-up flourish on boot. */
    s_after_wake = WILI_MOOD_IDLE;
    enter(WILI_MOOD_WAKE, now);

    uint64_t last_us = time_us_64();
    bool first = true;
    uint8_t rot = 0;
    uint8_t last_glow = 255;

    while (true) {
        const uint64_t us = time_us_64();
        float dt = (float)(us - last_us) * 1e-6f;
        if (dt > 0.1f) dt = 0.1f;
        last_us = us;
        now = (uint32_t)(us / 1000u);
        const float t = (float)(us % 600000000ull) * 1e-6f;

        /* envelopes: fast attack, slower release */
        const float mouth = (float)g_face.mouth / 255.0f;
        s_mouth_env = approach(s_mouth_env, mouth, mouth > s_mouth_env ? 30.0f : 14.0f, dt);
        const float mic = (float)g_face.mic / 255.0f;
        s_mic_env = approach(s_mic_env, mic, mic > s_mic_env ? 20.0f : 5.0f, dt);

        if ((g_face.rotation & 3u) != rot) {
            /* The board was turned. Turn the screen at once, but draw the face
               turned by the same amount, so the picture doesn't jump; then
               swing it upright (turn_angle). */
            const uint8_t was = rot;
            rot = g_face.rotation & 3u;
            const float now_ang = turn_angle(now);
            const unsigned dq = (unsigned)(rot - was) & 3u;   /* quarter turns clockwise */
            float from = now_ang + (dq == 3u ? -1.0f : (float)dq) * 1.5707963f;
            if (from > 3.2f) from -= 6.2831853f;
            if (from < -3.2f) from += 6.2831853f;
            st7789_set_rotation(rot);
            apply_layout();
            s_turn_from = from;
            s_turn_ms = now;
            /* a funny face, unless it's busy listening, thinking or talking */
            s_turn_funny = s_mood != WILI_MOOD_LISTEN && s_mood != WILI_MOOD_THINK &&
                           s_mood != WILI_MOOD_TALK;
            s_activity_ms = now;
            if (s_mood == WILI_MOOD_SLEEP || s_mood == WILI_MOOD_BORED) enter(WILI_MOOD_IDLE, now);
            first = true;   /* repaint every band in the new orientation */
        }
        set_turn(turn_angle(now));
        s_clip_screen = s_xf ? (clip_t){-200, -200, s_w + 200, s_h + 200} : (clip_t){0, 0, s_w, s_h};

        update_mood(now);
        target_pose(now, t);
        ease_pose(dt);
        compose(now, t, blink_open(now));
        draw_frame(first);
        first = false;

        const uint8_t glow = (uint8_t)fclampf(P.glow, 20.0f, 255.0f);
        if (glow != last_glow) {
            board_backlight(glow);
            last_glow = glow;
        }
        g_face.frames++;

        const uint32_t spent = (uint32_t)((time_us_64() - us) / 1000u);
        if (spent < FRAME_MIN_MS) sleep_ms(FRAME_MIN_MS - spent);
    }
}
