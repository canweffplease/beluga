#include <jack/jack.h>
#include <jack/ringbuffer.h>
#include <sndfile.h>

#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "metronome.h"

#define CHUNK_FLOATS 4096 //reader thread disk read size
#define MAX_TRACKS 16 // TODO: put this in a .env file as a shared constant
#define MAX_NFRAMES 8192 // should be more than enough head room 

// TODO: make a 'header file for this' ? 
typedef struct { 
    SNDFILE *sf;
    sf_count_t file_frames;
    jack_ringbuffer_t *ringbuffer;
    float gain;
    atomic_int reader_done; // set by reader once the last frame of the file is in the ring
    atomic_ulong played_frames; // frames of this track sent to mix
    unsigned long frames_read; // reader thread (and prefill) only
}   track_t;

static jack_client_t *client;
static jack_port_t *out_port;
static track_t tracks[MAX_TRACKS];
static int track_count = 0;

static float temp_buffer[MAX_NFRAMES];
static float mix_gain = 1.0f; // TODO: 1/track_count so no clip

//stats for callback and xrun
static atomic_ulong dropout_frames = 0; //frames of silence inserted because ring was empty
static atomic_ulong xruns = 0; //deadline misses

//reader thread stuff
static pthread_t reader;
static pthread_mutex_t wake_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t wake_cond = PTHREAD_COND_INITIALIZER;
static atomic_int reader_stop = 0; // set on early exit
static atomic_int finished = 0; // set by callback once every frame has been played

static volatile sig_atomic_t running = 1;

static metronome_t metronome;

static void stop_running(int sig) {  //interrupt that stops playback
    (void)sig;
    running = 0;
}

// move as much of the file as fits into the ring buffer
// called from main (prefill) before the reader thread exists, then only from the reader thread
// returns number of floats added
static size_t fill_ring(track_t *track) {
    float chunk[CHUNK_FLOATS];
    size_t total = 0;

    while (1) {
        size_t floats_free = jack_ringbuffer_write_space(track->ringbuffer) / sizeof(float);
        if (floats_free == 0) // ring is full
            return total; 

        size_t n = floats_free;
        if (n >= CHUNK_FLOATS) {
            n = CHUNK_FLOATS;
        }
        sf_count_t read_float = sf_read_float(track->sf, chunk, (sf_count_t)n);
        if (read_float <= 0) { // end of file or read error
            atomic_store_explicit(&track->reader_done, 1, memory_order_release);
            return total;
        }
        jack_ringbuffer_write(track->ringbuffer, (const char *)chunk, (size_t)read_float * sizeof(float));
        track->frames_read += (unsigned long)read_float;
        total += (size_t)read_float;
    }
}

//jack process callback
static int process(jack_nframes_t nframes, void *arg) {
    (void)arg;
    float *out = jack_port_get_buffer(out_port, nframes);
    memset(out, 0, nframes * sizeof(float));

    if (nframes > MAX_NFRAMES)
        return 0;

    int done[MAX_TRACKS];
    size_t floats_avail[MAX_TRACKS];
    int ready = 1;
    for (int i = 0; i < track_count; i ++) {
        done[i] = atomic_load_explicit(&tracks[i].reader_done, memory_order_acquire);
        floats_avail[i] = jack_ringbuffer_read_space(tracks[i].ringbuffer) / sizeof(float);
        
        if (!done[i] && floats_avail[i] < nframes) {
            ready = 0;
        }
    }

    if (!ready) {
        atomic_fetch_add_explicit(&dropout_frames, nframes, memory_order_relaxed);
    }
    else {
        int tracks_finished = 0;
        for (int i = 0; i < track_count; i++) {
            track_t *track = &tracks[i];
            size_t n = floats_avail[i];
            if (n > nframes) {
                n = nframes;
            }
            if (n > 0) {
                jack_ringbuffer_read(track->ringbuffer, (char *)temp_buffer, n * sizeof(float));
                float gain = mix_gain * track->gain;
                for (size_t j = 0; j < n; j ++)
                {
                    out[j] += temp_buffer[j] * gain;
                }
            }
            atomic_fetch_add_explicit(&track->played_frames, n, memory_order_relaxed);
            if (done[i] && floats_avail[i] <= nframes)
                tracks_finished ++;
        }
        metronome_mix(&metronome, out, nframes);
        if (tracks_finished == track_count) { // every track is over
            atomic_store_explicit(&finished, 1, memory_order_release);
        }

        
    }
    if (pthread_mutex_trylock(&wake_lock) == 0) { // wake up reader thread, space just opened up
        pthread_cond_signal(&wake_cond);
        pthread_mutex_unlock(&wake_lock);
    }
    return 0;
}

//jack xrun callback
static int on_xrun(void *arg) {
    (void)arg;
    atomic_fetch_add_explicit(&xruns, 1, memory_order_relaxed);
    return 0;
}

// reader thread callback
static void *reader_main(void *arg) {
    (void)arg;

    while (!atomic_load_explicit(&reader_stop, memory_order_acquire)) {
        int all_done = 1;
        for (int i = 0; i < track_count; i ++) {
            track_t *track = &tracks[i];
            if (atomic_load_explicit(&track->reader_done, memory_order_acquire))
                continue; // file in ring
            fill_ring(track);
            if (!atomic_load_explicit(&track->reader_done, memory_order_acquire))
                all_done = 0;
        }
        if (all_done) // every file on its ring
            break;

        struct timespec spec;
        clock_gettime(CLOCK_REALTIME, &spec);
        spec.tv_nsec += 20 * 1000 * 1000; // wake up thread after 20 ms
        if (spec.tv_nsec >= 1000000000L) { //reset nanosecond after second for pthread_cond_timewait
            spec.tv_sec++;
            spec.tv_nsec -= 1000000000L;
        }

        pthread_mutex_lock(&wake_lock);
        pthread_cond_timedwait(&wake_cond, &wake_lock, &spec);
        pthread_mutex_unlock(&wake_lock);
    }
    return NULL;
}

static const char *parse_track_arg(const char *arg, float *gain) {
    //gain:path
    char *end;
    double gain_arg = strtod(arg, &end); // end ptr is first character that cant be converted to d
    if (end != arg && *end == ':') {
        *gain = (float)gain_arg;
        return end + 1;
    }
    *gain = 1.0f;
    return arg;
}

// TODO: finish main for multitrack, left off on reading new arguments 
int main(int argc, char **argv) {
    if (argc < 6) {
        fprintf(stderr, "play error: %s [ring_seconds=2] [bpm, 0 = off] [dest_port] [dest_port2] [track1.wav] ... [trackn.wav]\n If you only want one destination port, use the character - for one\n",argv[0]);
        return 1;
    }

    double ring_secs = atof(argv[1]);
    const char *dest_port = (argv[3][0] && strcmp(argv[3], "-") != 0) ? argv[3] : NULL; // playback port
    const char *dest_port2 = (argv[4][0] && strcmp(argv[4], "-") != 0) ? argv[4] : NULL; // optional second port (mono -> both ears)
    //double ring_secs = (argc > 4) ? atof(argv[4]) : 2.0; //adjustable for more tolerance to possible disk stalls
    double bpm = atof(argv[2]);
    if (!metronome_bpm_ok(bpm)) {
        fprintf(stderr, "play error: bpm must be 0 (off) or between 20 and 300\n");
        return 1;
    }
    track_count = argc - 5;
    if (track_count < 1 || track_count > MAX_TRACKS) {
        fprintf(stderr, "play error: must provide at least 1 track and at most %d\n", MAX_TRACKS);
        return 1;
    }
    mix_gain = 1.0f / (float)track_count;

    jack_status_t status;
    client = jack_client_open("beluga_play", JackNullOption, &status);
    if (!client) {
        fprintf(stderr, "play error: jack_client_open failed (0x%x)\n", status);
        return 1;
    }

    unsigned sample_rate = jack_get_sample_rate(client);
    unsigned buff_size = jack_get_buffer_size(client);

    metronome_init(&metronome, bpm, sample_rate);

    size_t ring_bytes = (size_t)(ring_secs * sample_rate) * sizeof(float);
    if (ring_bytes < buff_size * sizeof(float) * 2) { // safety net if ring sec specified is too low, 2 periods
        ring_bytes = buff_size * sizeof(float) * 2;
    }
    
    sf_count_t longest = 0; // grab longest track frames for size of whole
    for (int i = 0; i < track_count; i++) {
        track_t *track = &tracks[i];
        //const char *filename = argv[4 + i];
        float gain;
        const char *filename = parse_track_arg(argv[5 + i], &gain);
        if (!(gain >= 0.0f && gain <= 2.0f)) {
            fprintf(stderr, "play error: gain for %s must be between 0 and 2\n", filename);
            return 1;
        }
        track->gain = gain;

        SF_INFO info;
        memset(&info, 0, sizeof(info));
        track->sf = sf_open(filename, SFM_READ, &info);
        if (!track->sf) {
            fprintf(stderr, "play error: sf_open %s: %s\n", filename, sf_strerror(NULL));
            return 1;
        }
        if (info.channels != 1) {
            fprintf(stderr, "play error: %s has %d channels, only mono is supported for now\n", filename, info.channels);
            sf_close(track->sf);
            return 1;
        }
        if ((unsigned)info.samplerate != sample_rate) {
            fprintf(stderr, "play warning: %s: %dHz, playback set to %uHz\n",filename, info.samplerate, sample_rate);
        }
        track->file_frames = info.frames;
        if (info.frames > longest) {
            longest = info.frames;
        }
        track->ringbuffer = jack_ringbuffer_create(ring_bytes);
        if (!track->ringbuffer) {
            fprintf(stderr, "play error: ringbuffer create failed\n");
            return 1;
        }
        if (jack_ringbuffer_mlock(track->ringbuffer)) {
            fprintf(stderr, "play warning: mlock failed ... continuing\n");
        }
    }

    out_port = jack_port_register(client, "out_1", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    if (!out_port) {
        fprintf(stderr, "play error: port register failed\n");
        return 1;
    }

    jack_set_process_callback(client, process, NULL);
    jack_set_xrun_callback(client, on_xrun, NULL);

    for (int i = 0; i < track_count; i ++ ) {
        fill_ring(&tracks[i]);
    }
    pthread_create(&reader, NULL, reader_main, NULL);

    if (jack_activate(client)) {
        fprintf(stderr, "play error: activate failed\n");
        return 1;
    }
    // out_1 is the source cuz jack goes out -> in
    if (dest_port && jack_connect(client, "beluga_play:out_1", dest_port)) {
        fprintf(stderr, "play warning: could not connect to %s\n", dest_port);
    }
    if (dest_port2 && jack_connect(client, "beluga_play:out_1", dest_port2)) {
        fprintf(stderr, "play warning: could not connect to %s\n", dest_port2);
    }

    signal(SIGINT, stop_running);
    printf("Playing %d track(s)  (%u Hz, buffer %u frames, ring %.1f s, %.2f s long). Ctrl-C to stop.\n", track_count, sample_rate, buff_size, (double)ring_bytes / sizeof(float) / sample_rate, (double)longest / sample_rate);

    while (running && !atomic_load_explicit(&finished, memory_order_acquire)) { // until file ends or Ctrl-C
        usleep(50000);
    }

    //cleanup
    int completed = atomic_load(&finished);
    jack_deactivate(client);
    atomic_store_explicit(&reader_stop, 1, memory_order_release);
    pthread_mutex_lock(&wake_lock);
    pthread_cond_signal(&wake_cond);
    pthread_mutex_unlock(&wake_lock);
    pthread_join(reader, NULL);

    for (int i = 0; i < track_count; i ++) {
        sf_close(tracks[i].sf);
    }

    jack_client_close(client);
    for (int i = 0; i < track_count; i++) {
        jack_ringbuffer_free(tracks[i].ringbuffer);
    }

    int test = 0; // for total_played != (unsigned long)info.frames

    printf("\n-=- summary -=-\n");
    printf("tracks          : %d\n", track_count);
    if (metronome.bpm > 0.0) {
        printf("Metronome on at %.1f bpm\n", metronome.bpm);
    }
    for (int i = 0; i < track_count; i ++) {
        unsigned long total_played = atomic_load(&tracks[i].played_frames); 
        track_t *track = &tracks[i];
        printf("track           :%d\n", i + 1);
        printf("file frames     : %lld (%.2f s)\n", (long long)track->file_frames, (double)track->file_frames / sample_rate);
        printf("read frames     : %lu\n", track->frames_read);
        printf("played frames   : %lu (%.2f s)\n", total_played, (double)total_played / sample_rate);
        printf("gain            : %.2f\n", track->gain);
        if (total_played != (unsigned long)track->file_frames) {
            test = 1;
        }

    }
    
    printf("dropout frames  : %lu  (silence inserted, ring ran dry)\n", atomic_load(&dropout_frames));
    printf("xruns           : %lu  (JACK deadline misses)\n", atomic_load(&xruns));
    if (completed && test != 0) {
        printf("play warning: played != file frames\n");
    }
    return 0;
}