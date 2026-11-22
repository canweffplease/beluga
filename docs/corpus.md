# RAG Corpus 

## Pipeline
 1. Someone describes sound in words (warm, spacious vocals)
 2. System retrieves relevant context
 3. LLM outputs JSON effect parameters that beluga can apply

## Data
 1. Descriptor to parameter data
     - SocialFX / Audealize-style data
 2. Own effect and parameter reference
     - For every effect beluga supports
        - need parameter names, units, ranges, and defaults + a one line meaning
 3. Semantic audio vocab
     - words to frequency regions and effects
        - like muddy = building around 200-400 Hz
        - harsh =2-5kHz
        - include reverb time andcompression ratio 
 4. instrument and context specific guidance
     - words can mean different thing on vox guitar and drums
 5. small set of worked json examples

## Paramters in Spotify Pedalboard API
| Group | Effects|
| ----------- | ----------- |
| Tone (EQ) | HighpassFilter, LowpassFilter, LowShelfFilter, HighShelfFilter, PeakFilter | 
| Dynamics | Compressor, Limiter, NoiseGate, Gain |
| Space and time | Reverb, Delay, Chorus, Phaser | 
| Color | Distortion, Clipping, Bitcrush |