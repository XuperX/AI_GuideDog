# SeeAI

## The problem

A blind or low-vision person standing at a curb cannot see whether the way ahead is empty, whether something is coming closer, or whether something is already in the way. A wrong “go” is dangerous. A cane, a crossing signal they can hear, or a person with them still has to make the final check. A camera recording from somewhere else cannot see the place they are standing.

SeeAI uses the camera they are holding. About every 4 seconds it describes what is in the frame, then chooses one signal from that description:

- **Clear** only when the way ahead is empty, or the only things are parked or off to the side.
- **Wait** when a hazard is approaching but is not yet in the way.
- **Stop** when something is already in the way, or the description leaves doubt.

It does not say to cross, step off a curb, or enter a road.
