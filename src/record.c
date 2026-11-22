#include <jack/jack.h>
#include <jack/ringbuffer.h>
#include <sndfile.h>

#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>

#include "metronome.h"

#define CHUNK_FLOATS 4096 //writer thread disk write size
#define MAX_TRACKS 16

typedef struct {
    jack_port_t *in_port;
    jack_ringbuffer_t *ringbuffer;
    SNDFILE *sf;
    unsigned long frames_written; // doesnt need lock
    unsigned long float_write_fails;
} track_t; // track struct for multiple tracks

static jack_client_t *client;
static track_t tracks[MAX_TRACKS];
static int track_count = 0;

//stats for callback and xrun
static atomic_ulong captured_frames = 0; //frames successfully added to ring
static atomic_ulong dropped_frames = 0; //frames dropped if ring buffer full
static atomic_ulong xruns = 0; //deadline misses

//writer thread stuff
static pthread_t writer;
static pthread_mutex_t wake_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t wake_cond = PTHREAD_COND_INITIALIZER;
static atomic_int writer_stop = 0; // set once producer stopped 

static volatile sig_atomic_t running = 1;

static jack_port_t *click_out_port;
static metronome_t metronome;

static void stop_running(int sig) {  //interrupt that stops recording
    (void)sig; 
    running = 0; 
} 

static int process(jack_nframes_t nframes, void *arg) {
    (void)arg;
    float *click_out = jack_port_get_buffer(click_out_port, nframes);
    memset(click_out, 0, nframes * sizeof(float));
    metronome_mix(&metronome, click_out, nframes);
    size_t bytes = nframes * sizeof(float);

    int space_flag = 1;
    for (int i = 0; i < track_count; i++) { // check if any ring has not enough space
        track_t *track = &tracks[i];
        if (jack_ringbuffer_write_space(track->ringbuffer) < bytes) {
            space_flag = 0;
        }
    }
    if (space_flag) {
        for (int i = 0; i < track_count; i ++) {
            track_t *track = &tracks[i];
            const float *in = jack_port_get_buffer(track->in_port, nframes);
            jack_ringbuffer_write(track->ringbuffer, (const char *)in, bytes);
        }
        atomic_fetch_add_explicit(&captured_frames, nframes, memory_order_relaxed); // count frames added into captured
    }
    else {
        atomic_fetch_add_explicit(&dropped_frames, nframes, memory_order_relaxed); // count frames dropped into dropped
    }

    if (pthread_mutex_trylock(&wake_lock) == 0) { // wake up writer thread after attempt write
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


// writer thread callback 
static void *writer_main(void *arg) {
    (void)arg;
    float chunk[CHUNK_FLOATS];

    while(1) {
        int stopping = atomic_load_explicit(&writer_stop, memory_order_acquire);
        int has_written = 0; // check to load stopping

        for (int i = 0; i < track_count; i ++) {
            track_t *track = &tracks[i];
            size_t floats_avail = jack_ringbuffer_read_space(track->ringbuffer) / sizeof(float); //number of bytes available for reading
            
            if (floats_avail == 0)
                continue;

            size_t n = floats_avail;
            if (n >= CHUNK_FLOATS) {
                n = CHUNK_FLOATS;
            }
            jack_ringbuffer_read(track->ringbuffer, (char *)chunk, n * sizeof(float)); // should write every time bytes are available for reading
            sf_count_t float_wrote = sf_write_float(track->sf, chunk, (sf_count_t)n); // write bytes to sound file
            if ((unsigned long)float_wrote < n) {
                printf("record warning: frames did not get written to sound file\n");
                track->float_write_fails ++;
            }
            track->frames_written += (unsigned long)float_wrote;
            has_written = 1;
        }
        

        if (!has_written) { // if nothing in buffer
            if (stopping) break; // we have read all and can return

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
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc < 8) { // change to 4 once taking in user ports
        fprintf(stderr, "record error: %s [session_dir] [seconds=0 (quit by Ctrl-C)] [ring_seconds=2] [out_port] [out_port2] [port1] ... [portn]\n", argv[0]);
        return 1;
    }
    const char *session_dir = argv[1]; // directory to store session wavs
    double seconds = (argc > 2) ? atof(argv[2]) : 0.0; 
    //const char *source_port = (argc > 3 && argv[3][0]) ? argv[3] : NULL; // source port
    double ring_secs = (argc > 3) ? atof(argv[3]) : 2.0; //adjustable for more tolerance to possible disk stalls
    double bpm = atof(argv[4]);
   if (!metronome_bpm_ok(bpm)) {
       fprintf(stderr, "record error: bpm must be 0 (off) or between 20 and 300\n");
       return 1;
    }
    const char *click_dest = (argv[5][0] && strcmp(argv[5], "-") != 0) ? argv[5] : NULL;
    const char *click_dest2 = (argv[6][0] && strcmp(argv[6], "-") != 0) ? argv[6] : NULL;
    track_count = argc - 7; // to get port count
    if (track_count < 1 || track_count > MAX_TRACKS) {
        fprintf(stderr, "record error: must provide atleast 1 input and at most %d\n", MAX_TRACKS);
        return 1;
    }

    if (mkdir(session_dir, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "record error: failed to create %s\n", session_dir);
        return 1;
    }

    jack_status_t status;
    client = jack_client_open("beluga_record", JackNullOption, &status);
    if (!client) { 
        fprintf(stderr, "record error: jack_client_open failed (0x%x)\n", status); 
        return 1; 
    }

    unsigned sample_rate = jack_get_sample_rate(client);
    unsigned buff_size = jack_get_buffer_size(client);

    metronome_init(&metronome, bpm, sample_rate);

    size_t ring_bytes = (size_t)(ring_secs * sample_rate) * sizeof(float);
    if (ring_bytes < buff_size * sizeof(float) * 2) { // safety net if ring sec specified is too low, 2 periods
        ring_bytes = buff_size * sizeof(float) * 2;
    }


    for (int i = 0; i < track_count; i ++) {
        track_t *track = &tracks[i];
        char filename[500]; // TODO: prob need to add buffer overflow check for these?
        char portname[40]; // TODO: ^
        snprintf(filename, sizeof(filename), "%s/track_%d.wav", session_dir, i + 1);
        snprintf(portname, sizeof(portname), "in_%d", i + 1);
        track->ringbuffer = jack_ringbuffer_create(ring_bytes);
        if (!track->ringbuffer) { 
            fprintf(stderr, "record error: ringbuffer create failed\n"); 
            return 1; 
        }
        if (jack_ringbuffer_mlock(track->ringbuffer)) {
            fprintf(stderr, "record warning: mlock failed ... continuing\n");
        }

        SF_INFO info;
        memset(&info, 0, sizeof(info));
        info.samplerate = (int)sample_rate;
        info.channels = 1;
        info.format = SF_FORMAT_WAV | SF_FORMAT_FLOAT;
        track->sf = sf_open(filename, SFM_WRITE, &info);
        if (!track->sf) { 
            fprintf(stderr, "record error: sf_open: %s\n", sf_strerror(NULL)); 
            return 1; 
        }

        track->in_port = jack_port_register(client, portname, JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
        if (!track->in_port) { 
            fprintf(stderr, "record error: port register failed for %s (track %d)\n", portname, i + 1);
            return 1; 
        }
    }

    click_out_port = jack_port_register(client, "click_out", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    if (!click_out_port) {
       fprintf(stderr, "record error: click port register failed\n");
       return 1;
    }

    jack_set_process_callback(client, process, NULL);
    jack_set_xrun_callback(client, on_xrun, NULL);

    pthread_create(&writer, NULL, writer_main, NULL);

    if (jack_activate(client)) { 
        fprintf(stderr, "record error: activate failed\n"); 
        return 1; 
    }

    if (bpm > 0.0) {
        if (click_dest && jack_connect(client, "beluga_record:click_out", click_dest)) {
            fprintf(stderr, "record warning: could not connect to %s\n", click_dest);
        }
        if (click_dest2 && jack_connect(client, "beluga_record:click_out", click_dest2)) {
            fprintf(stderr, "record warning: could not connect to %s\n", click_dest2);
        }
    }

    // connect ports to respective tracks
    // TODO: make user able to select which tracks, this is just for viability of multitrack recording
    for (int i = 0; i < track_count; i++) {
        const char *source_port = argv[7+i]; // TODO: research if this could lead to access overflow
        if (strcmp(source_port, "-") == 0) // signal for empty port
        {
            continue;
        }
        char destination[200];
        snprintf(destination, sizeof(destination), "beluga_record:in_%d", i + 1);
        if (source_port && jack_connect(client, source_port, destination)) {
            fprintf(stderr, "record warning: could not connect %s\n", source_port);
        }
    }
    
    signal(SIGINT, stop_running);
    printf("Recording %d tracks to %s  (%u Hz, buffer %u frames, ring %.1f s). Ctrl-C to stop.\n", track_count, session_dir, sample_rate, buff_size, (double)ring_bytes / sizeof(float) / sample_rate);

    unsigned long target = 0;
    if (seconds > 0) {
        target = (unsigned long)(seconds * sample_rate);
    }
    while (running) { // once target frames reached, stop recording
        usleep(50000);
        if (target && atomic_load(&captured_frames) + atomic_load(&dropped_frames) >= target)
            break;
    }

    //cleanup
    jack_deactivate(client);
    atomic_store_explicit(&writer_stop, 1, memory_order_release);
    pthread_mutex_lock(&wake_lock);
    pthread_cond_signal(&wake_cond);
    pthread_mutex_unlock(&wake_lock);
    pthread_join(writer, NULL);

    for (int i = 0; i < track_count; i++) {
        track_t *track = &tracks[i];
        sf_close(track->sf);
        jack_ringbuffer_free(track->ringbuffer);
    }

    jack_client_close(client);
    

    unsigned long total_captured = atomic_load(&captured_frames);
    unsigned long total_dropped = atomic_load(&dropped_frames);
    printf("\n-+- summary -+-\n");
    printf("captured frames     : %lu (%.2f s)\n", total_captured, (double)total_captured / sample_rate);
    printf("dropped frames      : %lu  (ring overruns)\n", total_dropped);
    printf("xruns               : %lu  (JACK deadline misses)\n", atomic_load(&xruns));
    if (bpm > 0.0) 
        printf("metronome on at %.1f bpm\n", bpm);
    for (int i = 0; i < track_count; i++) {
        track_t *track = &tracks[i];
        printf("\n-+- track[%d] -+-\n", i);
        printf("written frames      : %lu\n", track->frames_written);
        printf("float writes failed : %lu\n", track->float_write_fails);
        
        if (track->frames_written != total_captured) {
            printf("record warning: written != captured\n");
        }
    }
    
    return 0;
}