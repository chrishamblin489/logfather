# Machine reference for the simulator

The 3D scene is a simplification of the real cell, not its CAD: an infeed
conveyor, the gate the products line up against, a simplified AUBO i10 arm
(dimensions from the AUBO datasheet, in `../src/aubo_i10.js`) carrying the
vacuum-cup array, and the tray change system.

Anything that helps get the proportions and layout right can go here:

1. Photos or a sketch: whole cell from the front, the side and above; the
   vacuum head; where the tray sits relative to the belt and the arm base.
2. Key dimensions in mm: belt width, length and height, gate position, arm
   base position and height, tray/crate internal size(s).
3. Real figures: typical pick-and-place cycle time, tray change time, belt
   speed, cups on the vacuum head, its weight (the i10 carries 10 kg).

Anything customer-confidential should stay out of git: say so and the file
gets added to `.gitignore`.
