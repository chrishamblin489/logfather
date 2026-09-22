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

test("simulation: products bunch up against the gate, nose to tail", () => {
  const sim = new Simulation({ infeedPpm: 60, productsPerPick: 99, perCrate: 99 });
  sim.step(20); // the arm never gets its 99, so the line just grows
  const { gateX, productLength } = sim.config;
  assert.ok(sim.lineLength() >= 5);
  sim.products.slice(0, sim.lineLength()).forEach((p, i) => {
    assert.ok(Math.abs(p.x - (gateX - productLength / 2 - i * productLength)) < 1e-6);
  });
  assert.equal(sim.stats.packed, 0);
});

test("simulation: the arm lifts a whole line at once into consecutive slots", () => {
  const sim = new Simulation({ infeedPpm: 40, productsPerPick: 4, perCrate: 12 });
  const events = sim.step(60);
  const picked = events.filter((e) => e.type === "picked");
  const placed = events.filter((e) => e.type === "placed");
  assert.ok(picked.length >= 3);
  assert.ok(picked.every((e) => e.ids.length === 4));
  assert.deepEqual(placed.slice(0, 4).map((e) => [e.crate, e.slots]),
    [[1, [0, 1, 2, 3]], [1, [4, 5, 6, 7]], [1, [8, 9, 10, 11]], [2, [0, 1, 2, 3]]]);
  // Front of the line first: ids leave in arrival order.
  assert.deepEqual(placed[0].ids, [1, 2, 3, 4]);
});

test("simulation: a last pick smaller than the array tops the crate up exactly", () => {
  const sim = new Simulation({ infeedPpm: 40, productsPerPick: 4, perCrate: 10 });
  const placed = sim.step(60).filter((e) => e.type === "placed" && e.crate === 1);
  assert.deepEqual(placed.map((e) => e.slots.length), [4, 4, 2]);
});

test("simulation: under capacity it packs what arrives and the belt never backs up", () => {
  const config = { infeedPpm: 30, productsPerPick: 4, perCrate: 12 };
  assert.ok(estimateCapacityPpm(config) > 30);
  const sim = new Simulation(config);
  sim.step(600);
  assert.equal(sim.stats.blockedS, 0);
  const overallPpm = (sim.stats.packed / sim.time) * 60;
  assert.ok(Math.abs(overallPpm - 30) < 1.5, `overall ppm ${overallPpm}`);
});

test("simulation: over capacity the line backs up and output matches the estimate", () => {
  const config = { infeedPpm: 200, productsPerPick: 4, perCrate: 12 };
  const sim = new Simulation(config);
  sim.step(600);
  assert.ok(sim.stats.blockedS > 0);
  const overallPpm = (sim.stats.packed / sim.time) * 60;
  const estimate = estimateCapacityPpm(config);
  // The estimate is the ceiling: a real line re-forms a little slower than ideal.
  assert.ok(overallPpm <= estimate * 1.01 && overallPpm > estimate * 0.9, `sim ${overallPpm} vs estimate ${estimate}`);
});

test("simulation: a slow belt, not the arm, can be what limits the rate", () => {
  const fast = estimateCapacityPpm({ beltSpeed: 400, robotCycleS: 2 });
  const slow = estimateCapacityPpm({ beltSpeed: 100, robotCycleS: 2 });
  assert.ok(slow < fast);
  const sim = new Simulation({ infeedPpm: 200, beltSpeed: 100, robotCycleS: 2 });
  sim.step(600);
  const overallPpm = (sim.stats.packed / sim.time) * 60;
  assert.ok(overallPpm <= slow * 1.01 && overallPpm > slow * 0.9, `sim ${overallPpm} vs estimate ${slow}`);
});

test("simulation: a full tray waits by the arm and is pushed out at the next tray's final lift", () => {
  const sim = new Simulation({ infeedPpm: 60, productsPerPick: 4, perCrate: 12, beltSpeed: 800 });
  const events = [];
  for (let i = 0; i < 9000; i++) for (const e of sim.step(0.01)) events.push(Object.assign({ at: sim.time }, e));
  const of = (type) => events.filter((e) => e.type === type);
  // The tray packed before the run waits at the end stop, so the first tray's final
  // lift already pushes one out; after that, one push per tray.
  assert.equal(of("eject")[0].number, 0);
  assert.ok(of("eject")[0].at < of("crateFull")[0].at);
  assert.ok([0, 1].includes(of("eject").length - of("crateFull").length));
  // Each push starts with the grip of the last lift into the tray then being filled.
  for (const push of of("eject")) {
    const grip = of("gripStart").find((g) => Math.abs(g.at - push.at) < 1e-9);
    assert.ok(grip && grip.slots[grip.slots.length - 1] === 11, `push at ${push.at}`);
  }
});

test("simulation: the tray change waits for a slow push to clear", () => {
  const quick = new Simulation({ infeedPpm: 200, productsPerPick: 4, perCrate: 4, ejectS: 0.5 });
  const slow = new Simulation({ infeedPpm: 200, productsPerPick: 4, perCrate: 4, ejectS: 8 });
  quick.step(120); slow.step(120);
  assert.ok(slow.stats.crates < quick.stats.crates);
});

test("simulation: every arrival is packed or still in the cell", () => {
  const sim = new Simulation({ infeedPpm: 60, spacing: "random", seed: 7 });
  sim.step(120);
  assert.equal(sim.stats.arrived, sim.stats.packed + sim.products.length);
});

test("simulation: same seed repeats exactly", () => {
  const run = () => {
    const sim = new Simulation({ infeedPpm: 50, spacing: "random", seed: 3 });
    sim.step(90);
    return sim.stats;
  };
  assert.deepEqual(run(), run());
});
