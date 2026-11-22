name of PreSonus audio interface channels: 
 -   Studio 68c Analog Surround 2.1:capture_FL
 -   Studio 68c Analog Surround 2.1:capture_FR
 -   Studio 68c Analog Surround 2.1:capture_LFE

Changed: 
 - Studio 68c Pro:capture_AUX0
 - Studio 68c Pro:capture_AUX1
 - Studio 68c Pro:capture_AUX2
 - Studio 68c Pro:capture_AUX3
 - Studio 68c Pro:capture_AUX4
 - Studio 68c Pro:capture_AUX5

 Playback: 
  -  redrightguy1@redrightguy1-G448:~/Desktop/RTES/beluga$ pw-jack jack_lsp | grep -i playback
  -  Studio 68c Pro:playback_AUX0
  -  Studio 68c Pro:playback_AUX1
  -  Studio 68c Pro:playback_AUX2
  -  Studio 68c Pro:playback_AUX3
  -  Studio 68c Pro:playback_AUX4
  -  Studio 68c Pro:playback_AUX5



##REPRODUCABILITY
PipeWire Version
    redrightguy1@redrightguy1-G448:~$ pw-cli --version
    pw-cli
    Compiled with libpipewire 1.6.2
    Linked with libpipewire 1.6.2
    redrightguy1@redrightguy1-G448:~$ uname -r
    7.0.0-38-generic

Original alias card name of Studio 68c
 - 233	alsa_card.usb-PreSonus_Studio_68c_0132B90D-00	alsa
 - pactl set-card-profile alsa_card.usb-PreSonus_Studio_68c_0132B90D-00 pro-audio
 - only 3 capture ports of my audio interface were showing: capture_FL; FR; LFE. Changed profile to "pro-audio" qto expose every channel as a raw numbered port.
 - sudo apt install pulseaudio-utils
 - pactl list cards short
 - pactl set-card-profile <card name> pro-audio


Proof of PipeWire Assumption 2
 - PipeWire allows for setting of buffer sizes. 
    - was able to set PIPEWIRE_LATENCY linux environment variable to "256/444444". Set up a JACK client session before and after setting latency. Observed change in default buffer size from 1024 frames to 256 frames, and the sample rate from 48000 Hz to 44444 Hz.


Solution for record exit fail: 
 - pgrep -a record; pgrep -a jackd
 - pkill -9 -f build/record; pkill jackd
 - make clean && make