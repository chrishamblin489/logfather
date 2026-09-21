const test = require("node:test");
const assert = require("node:assert/strict");
const { packPattern, estimateCapacityPpm, Simulation } = require("../src/engine.js");

test("packPattern: grid, layers and total", () => {
  const p = packPattern({ length: 600, width: 400, height: 200 }, { length: 190, width: 120, height: 90 }, { orientation: "along" });
  assert.equal(p.cols, 3);
  assert.equal(p.rows, 3);
  assert.equal(p.layers, 2);
  assert.equal(p.total, 18);
  assert.equal(p.slots.length, 18);
});

test("packPattern: auto turns the product when that holds more", () => {
  const crate = { length: 400, width: 300, height: 100 };
  const product = { length: 290, width: 130, height: 80 };
  assert.equal(packPattern(crate, product, { orientation: "along" }).perLayer, 2);
  const auto = packPattern(crate, product);
  assert.equal(auto.rotated, true);
  assert.equal(auto.perLayer, 3);
});

test("packPattern: gap, maxLayers and a product too big for the crate", () => {
  const crate = { length: 600, width: 400, height: 300 };
  const product = { length: 200, width: 200, height: 100 };
  assert.equal(packPattern(crate, product).perLayer, 6);
  assert.equal(packPattern(crate, product, { gap: 10 }).perLayer, 2);
  assert.equal(packPattern(crate, product, { maxLayers: 1 }).total, 6);
  assert.equal(packPattern(crate, { length: 700, width: 200, height: 100 }).total, 0);
});

test("packPattern: slots are centred and stay inside the crate", () => {
  const crate = { length: 600, width: 400, height: 200 };
  const product = { length: 190, width: 120, height: 90 };
  const p = packPattern(crate, product, { gap: 5 });
  const sumX = p.slots.reduce((s, slot) => s + slot.x, 0);
  assert.ok(Math.abs(sumX) < 1e-6);
  for (const slot of p.slots) {
    assert.ok(Math.abs(slot.x) + product.length / 2 <= crate.length / 2);
    assert.ok(Math.abs(slot.y) + product.width / 2 <= crate.width / 2);
    assert.ok(slot.z + product.height <= crate.height);
  }
});

test("simulation: keeps up when the infeed is under capacity", () => {
  const config = { infeedPpm: 30, robotCycleS: 1.2, crateChangeS: 6, perCrate: 12 };
  assert.ok(estimateCapacityPpm(config) > 30);
  const sim = new Simulation(config);
  sim.step(300);
  assert.equal(sim.stats.missed, 0);
  const overallPpm = (sim.stats.packed / sim.time) * 60;
  assert.ok(Math.abs(overallPpm - 30) < 1.5, `overall ppm ${overallPpm}`);
  // The 60 s window wobbles with where the crate changes fall inside it.
  assert.ok(Math.abs(sim.rollingPpm() - 30) < 6, `rolling ppm ${sim.rollingPpm()}`);
  assert.ok(sim.stats.crates >= 11);
});

test("simulation: over capacity with the infeed never holding, products are missed", () => {
  const config = { infeedPpm: 80, robotCycleS: 1.2, crateChangeS: 6, perCrate: 12, infeedHoldsDuringCrateChange: false };
  const sim = new Simulation(config);
  sim.step(300);
  assert.ok(sim.stats.missed > 0);
  assert.ok(sim.rollingPpm() <= estimateCapacityPpm(config) + 2);
});

test("simulation: every arrival is packed, missed or still in the cell", () => {
  const sim = new Simulation({ infeedPpm: 60, spacing: "random", seed: 7 });
  sim.step(120);
  const inCell = sim.products.length;
  assert.equal(sim.stats.arrived, sim.stats.packed + sim.stats.missed + inCell);
});

test("simulation: same seed repeats exactly", () => {
  const run = () => {
    const sim = new Simulation({ infeedPpm: 50, spacing: "random", seed: 3 });
    sim.step(90);
    return sim.stats;
  };
  assert.deepEqual(run(), run());
});

test("simulation: slots fill in order and the crate number advances", () => {
  const sim = new Simulation({ infeedPpm: 40, perCrate: 4 });
  const placed = sim.step(40).filter((e) => e.type === "placed");
  assert.deepEqual(placed.slice(0, 5).map((e) => [e.crate, e.slot]), [[1, 0], [1, 1], [1, 2], [1, 3], [2, 0]]);
});
