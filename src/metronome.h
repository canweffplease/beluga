#ifndef BELUGA_METRONOME_H
#define BELUGA_METRONOME_H

// metronome click shared by play.c and record.c
// needs -lm when linking (sin and exp)
//
//   metronome_init() once in main, before jack_activate
//   metronome_mix() once per jack cycle from the process callback
//
// the click is made once in init, so mix only compares and copies:
// no malloc, no printf, safe to call from the real-time callback

#include <math.h>
#include <string.h>

#define METRONOME_MAX_CLICK_FRAMES 8192 // 30 ms is enough up to 192 kHz
#define METRONOME_CLICK_GAIN 0.5f       // click volume, not scaled by the 1/N track mix
#define METRONOME_PI 3.14159265358979323846

typedef struct {
    double bpm;                  // 0 = off
    double beat_period;          // frames between clicks, not a whole number (130 bpm = 22153.8)
    double next_beat;            // song frame the next click starts on
    unsigned long long song_frame; // frames counted so far
    float click_sound[METRONOME_MAX_CLICK_FRAMES];
    int click_len;
    int click_pos;               // click_pos == click_len means no click is sounding
} metronome_t;


// 1 if bpm is 0 (off) or between 20 and 300. also rejects nan
static int metronome_bpm_ok(double bpm) {
    return bpm == 0.0 || (bpm >= 20.0 && bpm <= 300.0);
}


// fills in the click. bpm 0 leaves the metronome off
static void metronome_init(metronome_t *m, double bpm, unsigned int sample_rate) {
    memset(m, 0, sizeof(*m));
    m->bpm = bpm;
    if (bpm <= 0.0) {
        return;
    }

    m->beat_period = 60.0 * sample_rate / bpm;
    m->click_len = (int)(0.03 * sample_rate); // 30 ms
    if (m->click_len > METRONOME_MAX_CLICK_FRAMES) {
        m->click_len = METRONOME_MAX_CLICK_FRAMES;
    }
    for (int i = 0; i < m->click_len; i ++) {
        double t = (double)i / sample_rate;
        m->click_sound[i] = (float)(sin(2.0 * METRONOME_PI * 1000.0 * t) * exp(-t * 150.0)); // 1 kHz tone that fades out fast
    }
    m->click_pos = m->click_len; // no click sounding yet
    m->next_beat = 0.0;          // first click on the first frame
}


// adds the click into out (nframes samples) and moves the beat counter forward by nframes
// the caller decides when to call it: play only calls it on cycles where the tracks advance
static void metronome_mix(metronome_t *m, float *out, unsigned int nframes) {
    if (m->bpm <= 0.0) {
        return;
    }
    for (unsigned int j = 0; j < nframes; j ++) {
        if ((double)m->song_frame >= m->next_beat) { // time for a click
            m->click_pos = 0;
            m->next_beat += m->beat_period;
        }
        if (m->click_pos < m->click_len) {
            out[j] += m->click_sound[m->click_pos] * METRONOME_CLICK_GAIN;
            m->click_pos ++;
        }
        m->song_frame ++;
    }
}

#endif