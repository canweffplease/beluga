# Sync testing notes
## What is in sync?
 - Track alignment, round-trip latency, met click one space apart, no drift over long periods of time, playback alignment, behavior under stress
## Tests
### Track alignment
 - record 2 tracks from the same input and measure offset
### round trip
 - record with met 
 - offset = recorded onset - expected click position
 - repeat n runs and get mean and spread (jitter)
 - expect minimal jitter and constant offset
### click grid accuracy
 - check some non integer frames per beat, ie 30 seconds of click at 100 and 130 bpm
 - every gap should be within 1 frame of the period
### long run drift
 - observe drift on 10 minute recording
 - should be no drift, compare first and last click positions against ideal
### playback alignment
 - current test covers this
### under load
 - all tests with artificial stress on cpu
### buffer size
 - repeat all tests with different buffer sizes 