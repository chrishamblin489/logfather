// AUBO i10 geometry and a tool-pointing-down pose solver for the simulator.
// Dimensions are from the AUBO-i10 "Technical Specifications & Data Sheet"
// (aubo-usa.com, PRODUCT / WORKSPACE / END FLANGE / ROBOT BASE drawings).
// Pure logic, no DOM and no three.js. Units: millimetres, radians, seconds.
// Robot frame: origin at the centre of the mounting face, z up, J1 about z.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AuboI10 = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const DEG = Math.PI / 180;

  const SPEC = {
    name: "AUBO i10",
    payloadKg: 10,
    reach: 1350,              // J2 axis to the J5/J6 wrist, arm straight
    heightStraightUp: 1513,
    weightKg: 38.5,
    repeatability: 0.03,
    toolSpeedMax: 4000,       // mm/s
    links: {
      baseToShoulder: 163,    // mounting face up to the J2 axis
      upperArm: 647,          // J2 axis to J3 axis
      forearm: 600.5,         // J3 axis to J4 axis
      wrist: 102.5,           // J4 axis to the J6 axis, along J5
      flange: 94,             // J5 axis to the tool flange face, along J6
    },
    // Sideways steps between the link centrelines, along the J2/J3/J4 axes.
    offsets: { shoulder: 197, elbow: -123.5, wristSide: 127.8 },
    base: { footprintDia: 220, boltCircleDia: 185, boltHoles: 4, boltHoleDia: 11, flangeThickness: 17 },
    toolFlange: { outerDia: 75, registerDia: 63, boreDia: 31.5, boltCircleDia: 50, bolts: "4 x M6" },
    workspace: { sphereRadius: 1247.5, innerCylinderRadius: 307.5 },
    joints: [
      { name: "J1 Base", range: 360 * DEG, maxSpeed: 178 * DEG },
      { name: "J2 Shoulder", range: 360 * DEG, maxSpeed: 178 * DEG },
      { name: "J3 Elbow", range: 360 * DEG, maxSpeed: 223 * DEG },
      { name: "J4 Wrist", range: 360 * DEG, maxSpeed: 178 * DEG },
      { name: "J5 Wrist", range: 360 * DEG, maxSpeed: 237 * DEG },
      { name: "J6 Wrist", range: 360 * DEG, maxSpeed: 237 * DEG },
    ],
    // NOT on the datasheet: scaled off its side-view drawing, for looks only.
    approxDiameters: { baseJoint: 150, shoulderJoint: 175, upperArmTube: 115, elbowJoint: 130, forearmTube: 80, wristJoint: 94 },
  };

  // Pose with the tool flange at `target` {x, y, z}, facing straight down and
  // turned `yaw` about the vertical: what a vacuum head over a belt needs.
  // Elbow up. Returns the joint angles (zero = the datasheet's straight-up
  // pose; the signs are this model's, not the AUBO controller's) and the
  // points the renderer strings the links between.
  function solveToolDown(target, yaw) {
    const L = SPEC.links;
    const side = SPEC.offsets.shoulder + SPEC.offsets.elbow + SPEC.offsets.wristSide;
    const planar2 = target.x * target.x + target.y * target.y - side * side;
    if (planar2 <= 0) return { reachable: false, reason: "inside the column over the base" };
    const flangeRadial = Math.sqrt(planar2);
    const j1 = Math.atan2(target.y, target.x) - Math.atan2(side, flangeRadial);

    // Two-link problem in the arm's vertical plane, shoulder to the J4 axis.
    const r = flangeRadial - L.wrist;
    const h = target.z + L.flange - L.baseToShoulder;
    const d = Math.hypot(r, h);
    if (d > L.upperArm + L.forearm - 1e-9) return { reachable: false, reason: "beyond reach" };
    if (d < Math.abs(L.upperArm - L.forearm) + 1e-9) return { reachable: false, reason: "too close to the shoulder" };
    const upperElevation = Math.atan2(h, r)
      + Math.acos((L.upperArm * L.upperArm + d * d - L.forearm * L.forearm) / (2 * L.upperArm * d));
    const elbowR = L.upperArm * Math.cos(upperElevation);
    const elbowH = L.upperArm * Math.sin(upperElevation);
    const forearmElevation = Math.atan2(h - elbowH, r - elbowR);

    const radial = { x: Math.cos(j1), y: Math.sin(j1) };
    const lateral = { x: -Math.sin(j1), y: Math.cos(j1) };
    const at = (rad, lat, z) => ({
      x: radial.x * rad + lateral.x * lat,
      y: radial.y * rad + lateral.y * lat,
      z,
    });
    const zS = L.baseToShoulder;
    const o1 = SPEC.offsets.shoulder;
    const o2 = o1 + SPEC.offsets.elbow;
    const points = {
      base: at(0, 0, 0),
      shoulder: at(0, 0, zS),
      upperArmStart: at(0, o1, zS),
      upperArmEnd: at(elbowR, o1, zS + elbowH),
      forearmStart: at(elbowR, o2, zS + elbowH),
      forearmEnd: at(r, o2, zS + h),
      wrist1: at(r, side, zS + h),
      wrist2: at(r + L.wrist, side, zS + h),
      flange: at(r + L.wrist, side, zS + h - L.flange),
    };
    return {
      reachable: true,
      joints: [
        j1,
        upperElevation - Math.PI / 2,
        forearmElevation - upperElevation,
        -forearmElevation,
        Math.PI / 2,
        (yaw || 0) - j1,
      ],
      points,
    };
  }

  // Shortest time the joints allow for a move between two poses, each joint
  // at its datasheet top speed (no acceleration limits: a floor, not a claim).
  function minMoveTime(jointsA, jointsB) {
    let t = 0;
    for (let i = 0; i < 6; i++) {
      let delta = Math.abs(jointsB[i] - jointsA[i]) % (2 * Math.PI);
      if (delta > Math.PI) delta = 2 * Math.PI - delta;
      t = Math.max(t, delta / SPEC.joints[i].maxSpeed);
    }
    return t;
  }

  return { SPEC, solveToolDown, minMoveTime };
});
