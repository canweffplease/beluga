## STACK

 - Audio i/o: jack/pipewire, client, ring buffer, real-time callback; C, native libjack api
    - real time thread needs to be in C, no garbage collection should be anywhere near this thread
 - WAV file i/o: C, libsndfile
    - time sensitive so needs to be in c
 - Metronome: C, same process as the audio engine 
 - Tuner / pitch detection: C, aubio
    - both metronome and tuner also real time so C
 - Session/undo state (track list, gain/mute, command log): C with cjson or with python for faster iteration; Probably will use python
     - choosing python for easier json reference, faster build, and easier to interact with for CLI
 - Effect (eq, revert, distortion): Python, spotify’s pedalboard library
 - RAG (embedding and retrieval): Python
    - so can avoid hand-rolling json schema and can utilize sentence transforming libraries
 - CLI + orchestration: Python, will check out click library by same people who did flask; If click too limiting, then argparse
     - click is a good command line framework
