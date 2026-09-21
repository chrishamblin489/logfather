const test = require("node:test");
const assert = require("node:assert/strict");
const { SPEC, solveToolDown, minMoveTime } = require("../src/aubo_i10.js");

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
const near = (a, b, tol) => assert.ok(Math.abs(a - b) < (tol || 1e-6), `${a} vs ${b}`);

test("datasheet figures agree with each other", () => {
  const L = SPEC.links;
  near(L.upperArm + L.forearm + L.wrist, SPEC.reach);
  near(L.baseToShoulder + SPEC.reach, SPEC.heightStraightUp);
  near(L.upperArm + L.forearm, SPEC.workspace.sphereRadius);
});

test("solveToolDown: the flange lands on the target and the links keep their lengths", () => {
  for (const target of [{ x: 700, y: 0, z: 200 }, { x: -300, y: 650, z: 50 }, { x: 400, y: -500, z: 600 }]) {
    const pose = solveToolDown(target, 0.3);
    assert.ok(pose.reachable);
    const p = pose.points;
    near(dist(p.flange, target), 0);
    near(dist(p.upperArmStart, p.upperArmEnd), SPEC.links.upperArm);
    near(dist(p.forearmStart, p.forearmEnd), SPEC.links.forearm);
    near(dist(p.wrist1, p.wrist2), SPEC.links.wrist);
    near(dist(p.wrist2, p.flange), SPEC.links.flange);
    near(dist(p.shoulder, p.upperArmStart), SPEC.offsets.shoulder);
    near(dist(p.upperArmEnd, p.forearmStart), Math.abs(SPEC.offsets.elbow));
    near(dist(p.forearmEnd, p.wrist1), SPEC.offsets.wristSide);
    near(p.wrist2.z - p.flange.z, SPEC.links.flange);   // tool straight down
    assert.ok(p.upperArmEnd.z > p.shoulder.z);          // elbow up
  }
});

test("solveToolDown: out of reach and over the base are refused", () => {
  assert.equal(solveToolDown({ x: 1500, y: 0, z: 100 }).reachable, false);
  assert.equal(solveToolDown({ x: 100, y: 50, z: 300 }).reachable, false);
});

test("minMoveTime: the slowest joint sets the time, the short way round", () => {
  const a = [0, 0, 0, 0, 0, 0];
  near(minMoveTime(a, [Math.PI / 2, 0, 0, 0, 0, 0]), 90 / 178, 1e-9);
  near(minMoveTime(a, [0, 0, 0, 0, 0, 1.75 * Math.PI]), 45 / 237, 1e-9);
});
