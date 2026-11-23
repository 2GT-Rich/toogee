/* Wili's face: an animated, anthropomorphic face rendered on core 1.
 *
 * Core 0 writes g_face (mood requests, audio envelopes, pokes); core 1 owns
 * the LCD and runs face_core1_main() forever. Every field is single-writer,
 * so plain volatile is enough.
 */
#ifndef WILI_FACE_H
#define WILI_FACE_H

#include <stdint.h>

#include "hardware/sync.h"

#define WILI_CAPTION_MAX 24   /* characters of caption text (firmware 009+) */

typedef enum {
    WILI_MOOD_IDLE = 0,   /* awake, waiting: blinks, glances, breathes        */
    WILI_MOOD_LISTEN,     /* attentive, eyes react to the mic                 */
    WILI_MOOD_THINK,      /* looks up and away, thinking dots                 */
    WILI_MOOD_TALK,       /* mouth follows the speaker audio                  */
    WILI_MOOD_HAPPY,      /* ^ ^ eyes, blush, big smile (auto -> idle)        */
    WILI_MOOD_CONFUSED,   /* lopsided eyes, wavy mouth, "?" (auto -> idle)    */
    WILI_MOOD_SAD,        /* droopy eyes, frown, a tear (auto -> idle)        */
    WILI_MOOD_SLEEP,      /* closed eyes, Zzz, dim (also automatic)           */
    WILI_MOOD_BORED,      /* half-lidded, sighs and yawns (also automatic)    */
    WILI_MOOD_WAKE,       /* startled awake (auto -> listen)                  */
    WILI_MOOD_COUNT
} wili_mood_t;

typedef struct {
    volatile uint8_t  mood;       /* requested mood (core 0 writes)          */
    volatile uint32_t mood_seq;   /* bumped with every request               */
    volatile uint8_t  mouth;      /* 0..255 speaker envelope, now playing    */
    volatile uint8_t  mic;        /* 0..255 mic envelope                     */
    volatile uint32_t poke_seq;   /* bumped on a button press                */
    volatile uint8_t  shown;      /* mood core 1 is showing (core 1 writes)  */
    volatile uint32_t frames;     /* frames drawn (core 1 writes)            */
    volatile uint8_t  lcd_ok;     /* LCD came up (core 1 writes)             */
    volatile uint8_t  rotation;   /* quarter turns clockwise, 0..3 (core 0
                                     writes; core 1 turns the screen)        */
    volatile uint32_t caption_seq;  /* odd while core 0 is writing caption   */
    volatile char     caption[WILI_CAPTION_MAX + 1];   /* "" = none         */
} wili_face_t;

extern wili_face_t g_face;

/* Request a mood from core 0. */
static inline void face_set_mood(wili_mood_t m) {
    g_face.mood = (uint8_t)m;
    g_face.mood_seq++;
}

/* Set (or with len 0 clear) the line of text along the bottom of the screen,
   e.g. "sleeping... 29:41". Core 0 only; core 1 copies it when the sequence
   number is even and unchanged across the copy. */
static inline void face_set_caption(const uint8_t *p, unsigned len) {
    if (len > WILI_CAPTION_MAX) len = WILI_CAPTION_MAX;
    g_face.caption_seq++;
    __dmb();
    for (unsigned i = 0; i < len; i++)
        g_face.caption[i] = (p[i] >= 32u && p[i] < 127u) ? (char)p[i] : ' ';
    g_face.caption[len] = 0;
    __dmb();
    g_face.caption_seq++;
}

void face_core1_main(void);

#endif
