// PikPak simulator engine: pack patterns and the pick-and-place flow.
// Pure logic, no DOM and no three.js, so it runs under `node --test` and is
// inlined unchanged into the page. Units: millimetres and seconds.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PikPakEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Small seeded generator so a run (and a test) repeats exactly.
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function gridFit(space, item, gap) {
    if (item <= 0 || item > space) return 0;
    return Math.floor((space + gap) / (item + gap));
  }

  // How the product fills the crate. `crate` is the INTERNAL size.
  // orientation: "along" (product length along crate length), "across"
  // (turned 90 degrees) or "auto" (whichever holds more per layer).
  // Slot positions are crate-local: origin at the centre of the crate floor,
  // x along the crate length, y along its width, z up to the product's base.
  function packPattern(crate, product, options) {
    const opts = options || {};
    const gap = Math.max(0, opts.gap || 0);
    const orientation = opts.orientation || "auto";

    const along = {
      rotated: false, l: product.length, w: product.width,
      cols: gridFit(crate.length, product.length, gap),
      rows: gridFit(crate.width, product.width, gap),
    };
    const across = {
      rotated: true, l: product.width, w: product.length,
      cols: gridFit(crate.length, product.width, gap),
      rows: gridFit(crate.width, product.length, gap),
    };
    let chosen = along;
    if (orientation === "across") chosen = across;
    else if (orientation === "auto" && across.cols * across.rows > along.cols * along.rows) chosen = across;

    let layers = product.height > 0 ? Math.floor(crate.height / product.height) : 0;
    if (opts.maxLayers != null) layers = Math.min(layers, Math.max(0, opts.maxLayers));
    const perLayer = chosen.cols * chosen.rows;
    if (perLayer === 0) layers = 0;

    const slots = [];
    const spanX = chosen.cols * chosen.l + (chosen.cols - 1) * gap;
    const spanY = chosen.rows * chosen.w + (chosen.rows - 1) * gap;
    for (let layer = 0; layer < layers; layer++) {
      for (let row = 0; row < chosen.rows; row++) {
        for (let col = 0; col < chosen.cols; col++) {
          slots.push({
            index: slots.length, layer, row, col,
            x: -spanX / 2 + chosen.l / 2 + col * (chosen.l + gap),
            y: -spanY / 2 + chosen.w / 2 + row * (chosen.w + gap),
            z: layer * product.height,
            rotated: chosen.rotated,
          });
        }
      }
    }
    const used = perLayer * layers * product.length * product.width * product.height;
    const volume = crate.length * crate.width * crate.height;
    return {
      cols: chosen.cols, rows: chosen.rows, layers, perLayer,
      total: slots.length, rotated: chosen.rotated, slots,
      fillRatio: volume > 0 ? used / volume : 0,
    };
  }

  const DEFAULTS = {
    infeedPpm: 40,            // products per minute arriving
    spacing: "even",          // "even" | "random"
    beltSpeed: 250,           // mm/s
    beltLength: 2400,         // mm; a product passing the end was not packed
    pickZoneStart: 900,       // mm along the belt the arm can reach
    pickZoneEnd: 1700,
    productLength: 180,       // mm, sets the closest two products can sit
    robotCycleS: 1.2,         // one full pick-and-place cycle (planner_full_cycle_time)
    crateChangeS: 6,          // eject the full crate and bring in the next
    infeedHoldsDuringCrateChange: true, // belt and arrivals wait while the crate is swapped
    perCrate: 12,
    seed: 1,
  };

  // Packed products per minute the cell can sustain, crate changes included.
  function estimateCapacityPpm(config) {
    const c = Object.assign({}, DEFAULTS, config);
    if (c.perCrate <= 0 || c.robotCycleS <= 0) return 0;
    const perCrateS = c.perCrate * c.robotCycleS + c.crateChangeS;
    return (c.perCrate / perCrateS) * 60;
  }

  function Simulation(config) {
    this.config = Object.assign({}, DEFAULTS, config);
    this.reset();
  }

  Simulation.prototype.reset = function () {
    this.random = mulberry32(this.config.seed);
    this.time = 0;
    this.nextId = 1;
    this.products = [];       // on the belt or in the gripper
    this.nextArrival = 0;
    this.lastRelease = -Infinity;
    this.backlog = 0;         // products waiting upstream of the held infeed
    this.robot = { phase: "idle", elapsed: 0, duration: 0, product: null, slot: -1 };
    this.crate = { count: 0, changing: false, changeElapsed: 0, number: 1 };
    this.placeTimes = [];
    this.stats = { arrived: 0, packed: 0, missed: 0, crates: 0, busyS: 0 };
  };

  Simulation.prototype._arrivalGap = function () {
    const c = this.config;
    const mean = 60 / Math.max(c.infeedPpm, 1e-6);
    const min = this._minGap();
    if (c.spacing !== "random" || mean <= min) return Math.max(mean, min);
    return min + -Math.log(1 - this.random()) * (mean - min);
  };

  Simulation.prototype._minGap = function () {
    return (this.config.productLength * 1.1) / Math.max(this.config.beltSpeed, 1e-6);
  };

  // Advance by dt seconds; returns the events the renderer animates.
  Simulation.prototype.step = function (dt) {
    const events = [];
    let left = dt;
    while (left > 1e-9) {
      const h = Math.min(left, 0.01);
      this._tick(h, events);
      left -= h;
    }
    return events;
  };

  Simulation.prototype._tick = function (h, events) {
    const c = this.config;
    this.time += h;
    const infeedRunning = !(c.infeedHoldsDuringCrateChange && this.crate.changing);

    // Arrivals. While the infeed holds they queue upstream and follow on after.
    if (c.infeedPpm > 0) {
      while (this.nextArrival <= this.time) {
        this.backlog += 1;
        this.nextArrival += this._arrivalGap();
      }
    }
    if (infeedRunning && this.backlog > 0 && this.time - this.lastRelease >= this._minGap()) {
      const product = { id: this.nextId++, x: 0, state: "belt" };
      this.products.push(product);
      this.backlog -= 1;
      this.lastRelease = this.time;
      this.stats.arrived += 1;
      events.push({ type: "spawn", id: product.id });
    }

    // Belt. A targeted product still rides the belt until the gripper has it.
    if (infeedRunning) {
      for (const p of this.products) {
        if (p.state === "belt" || p.state === "targeted") p.x += c.beltSpeed * h;
      }
    }
    for (const p of this.products) {
      if (p.state === "belt" && p.x > c.beltLength) {
        p.state = "missed";
        this.stats.missed += 1;
        events.push({ type: "missed", id: p.id });
      }
    }
    this.products = this.products.filter((p) => p.state !== "missed" && p.state !== "placed");

    // Crate change.
    if (this.crate.changing) {
      this.crate.changeElapsed += h;
      if (this.crate.changeElapsed >= c.crateChangeS) {
        this.crate = { count: 0, changing: false, changeElapsed: 0, number: this.crate.number + 1 };
        events.push({ type: "crateIn", number: this.crate.number });
      }
    }

    // Robot.
    const r = this.robot;
    if (r.phase !== "idle") {
      r.elapsed += h;
      this.stats.busyS += h;
    }
    if (r.phase === "toPick" && r.elapsed >= r.duration) {
      r.product.state = "carried";
      r.phase = "toPlace";
      r.elapsed = 0;
      events.push({ type: "picked", id: r.product.id, x: r.product.x });
    } else if (r.phase === "toPlace" && r.elapsed >= r.duration) {
      r.product.state = "placed";
      this.crate.count += 1;
      this.stats.packed += 1;
      this.placeTimes.push(this.time);
      events.push({ type: "placed", id: r.product.id, slot: r.slot, crate: this.crate.number });
      r.phase = "idle";
      r.product = null;
      if (this.crate.count >= c.perCrate) {
        this.crate.changing = true;
        this.crate.changeElapsed = 0;
        this.stats.crates += 1;
        events.push({ type: "crateFull", number: this.crate.number });
      }
    }
    if (r.phase === "idle" && !this.crate.changing && c.perCrate > 0) {
      const half = c.robotCycleS / 2;
      // Furthest-downstream product the arm can still meet inside its reach.
      let best = null;
      for (const p of this.products) {
        if (p.state !== "belt") continue;
        const meetX = p.x + c.beltSpeed * half;
        if (meetX < c.pickZoneStart || meetX > c.pickZoneEnd) continue;
        if (!best || p.x > best.x) best = p;
      }
      if (best) {
        best.state = "targeted";
        r.phase = "toPick";
        r.elapsed = 0;
        r.duration = half;
        r.product = best;
        r.slot = this.crate.count;
        events.push({ type: "pickStart", id: best.id, slot: r.slot, meetX: best.x + c.beltSpeed * half });
      }
    }
  };

  // Packed products per minute over the trailing window (default 60 s).
  Simulation.prototype.rollingPpm = function (windowS) {
    const w = windowS || 60;
    const from = this.time - w;
    while (this.placeTimes.length && this.placeTimes[0] < from) this.placeTimes.shift();
    const span = Math.min(w, this.time);
    return span > 0 ? (this.placeTimes.length / span) * 60 : 0;
  };

  Simulation.prototype.utilisation = function () {
    return this.time > 0 ? this.stats.busyS / this.time : 0;
  };

  return { packPattern, estimateCapacityPpm, Simulation, DEFAULTS, mulberry32 };
});
