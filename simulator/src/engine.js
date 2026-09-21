// PikPak simulator engine: pack patterns and the gate, line and group-pick flow.
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

  // Slots for a layout the SKU file states outright: `rows` x `columns` per
  // layer (columns run along the tray length) and `layers` high, spread evenly
  // across the tray. `tray` is the INTERNAL size. `spare` is the room left
  // over in mm with every product at its nominal size: x along the tray
  // length, y across its width, z from the top of the stack up to the rim.
  // `layout.squeeze` is how many mm one product may give in each direction
  // (punnet rims flex, sides taper, film lids settle): a direction may run
  // short by up to that much per product in it (`allowance`). The layout fits
  // when spare + allowance is never below 0; `tight` says it needed the
  // squeeze. `slotSize` is the size each product is left with, for drawing.
  // A layout that does not fit still gets its slots so the clash can be drawn.
  function layoutPattern(tray, product, layout) {
    const rows = layout.rows, cols = layout.columns, layers = layout.layers;
    const squeeze = Math.max(0, layout.squeeze || 0);
    const spareAs = (l, w) => ({
      x: tray.length - cols * l,
      y: tray.width - rows * w,
      z: tray.depth - layers * product.height,
    });
    const along = spareAs(product.length, product.width);
    const across = spareAs(product.width, product.length);
    const tightest = (spare) => Math.min(spare.x, spare.y);
    const orientation = layout.orientation || "auto";
    // Auto keeps the product as it lies unless the turned layout has more room.
    const rotated = orientation === "across"
      || (orientation === "auto" && tightest(along) < 0 && tightest(across) > tightest(along));
    const spare = rotated ? across : along;
    const allowance = { x: cols * squeeze, y: rows * squeeze, z: layers * squeeze };
    const fits = ["x", "y", "z"].every((k) => spare[k] + allowance[k] >= 0);
    const slotSize = {
      length: Math.min(rotated ? product.width : product.length, tray.length / cols),
      width: Math.min(rotated ? product.length : product.width, tray.width / rows),
      height: fits ? Math.min(product.height, tray.depth / layers) : product.height,
    };
    const gapX = Math.max(0, spare.x / (cols + 1));
    const gapY = Math.max(0, spare.y / (rows + 1));
    const spanX = cols * slotSize.length + (cols - 1) * gapX;
    const spanY = rows * slotSize.width + (rows - 1) * gapY;
    const slots = [];
    for (let layer = 0; layer < layers; layer++) {
      for (let row = 0; row < rows; row++) {
        for (let col = 0; col < cols; col++) {
          slots.push({
            index: slots.length, layer, row, col,
            x: -spanX / 2 + slotSize.length / 2 + col * (slotSize.length + gapX),
            y: -spanY / 2 + slotSize.width / 2 + row * (slotSize.width + gapY),
            z: layer * slotSize.height,
            rotated,
          });
        }
      }
    }
    return {
      rows, cols, layers, perLayer: rows * cols, total: slots.length, rotated, slots,
      spare, allowance, fits, tight: fits && Math.min(spare.x, spare.y, spare.z) < 0,
      slotSize, stackHeight: layers * product.height,
    };
  }

  // The tray station: what the arm fills between two tray changes. It is one
  // full-size tray (about 600 x 400), or two half-size trays (about 400 x 300)
  // turned 90 degrees and set side by side so together they cover the same
  // footprint; the pair is filled and changed as one. Station coordinates:
  // origin at the centre of the station floor, x along its length (the 600),
  // y across. `stationSlots` are in fill order: layer by layer, in lines of
  // products the way the vacuum head sets them down. A line runs along x (and
  // carries on from the first half tray into the second) unless the pick size
  // only matches the rows across, as with 3 x 2 lifted three at a time.
  function stationPattern(tray, product, layout) {
    const base = layoutPattern(tray, product, layout);
    const trays = layout.traysSideBySide === 2 ? 2 : 1;
    const pitch = layout.trayPitch || 300;   // centre to centre of the two half trays
    const stationSlots = [];
    for (let k = 0; k < trays; k++) {
      for (const slot of base.slots) {
        stationSlots.push(trays === 1
          ? { tray: 0, layer: slot.layer, sRow: slot.row, sCol: slot.col, x: slot.x, y: slot.y, z: slot.z, rotated: slot.rotated }
          : { tray: k, layer: slot.layer, sRow: slot.col, sCol: k * base.rows + slot.row,
              x: (k - 0.5) * pitch + slot.y, y: slot.x, z: slot.z, rotated: !slot.rotated });
      }
    }
    const stationCols = trays === 1 ? base.cols : 2 * base.rows;
    const stationRows = trays === 1 ? base.rows : base.cols;
    const alongXPerTray = trays === 1 ? base.cols : base.rows;
    const perPick = layout.productsPerPick || alongXPerTray;
    const line = perPick === stationRows && perPick !== alongXPerTray && perPick !== stationCols ? "y" : "x";
    stationSlots.sort((a, b) => a.layer - b.layer
      || (line === "x" ? a.sRow - b.sRow || a.sCol - b.sCol : a.sCol - b.sCol || a.sRow - b.sRow));
    stationSlots.forEach((slot, index) => { slot.index = index; });
    return Object.assign({}, base, {
      trays, trayPitch: pitch, stationSlots, stationCols, stationRows, line,
      perTray: base.total, total: base.total * trays,
    });
  }

  // The flow: the belt runs products into a stop gate, where they bunch up
  // nose to tail (the belt slips underneath). Once `productsPerPick` of them
  // are pressed up in a line, the arm lowers an array of vacuum cups, one per
  // product, lifts the whole line at once and releases it into the crate.
  const DEFAULTS = {
    infeedPpm: 40,            // products per minute arriving
    spacing: "even",          // "even" | "random"
    beltSpeed: 250,           // mm/s
    gateX: 1800,              // mm from the belt start to the gate face
    productLength: 180,       // mm the product takes up along the belt
    productsPerPick: 4,       // vacuum cups in the array = products lifted together
    robotCycleS: 3,           // grip, move to the crate, release, move back
    gripS: 0.2,               // vacuum on (and again off) dwell, inside robotCycleS
    crateChangeS: 6,          // eject the full crate and bring in the next
    perCrate: 12,
    seed: 1,
  };

  function moveS(c) {
    return Math.max(0, (c.robotCycleS - 2 * c.gripS) / 2);
  }

  // Packed products per minute the cell can sustain. Two things pace a pick:
  // the arm's own cycle, and the line re-forming at the gate (the last product
  // of the next group has to travel a whole group length). The arm's return
  // move overlaps the crate change.
  function estimateCapacityPpm(config) {
    const c = Object.assign({}, DEFAULTS, config);
    const n = Math.min(c.productsPerPick, c.perCrate);
    if (n <= 0 || c.robotCycleS <= 0 || c.beltSpeed <= 0) return 0;
    const picks = Math.ceil(c.perCrate / n);
    // The line only starts closing up once the gripped products have lifted off.
    const reform = c.gripS + (n * c.productLength) / c.beltSpeed;
    const pickPeriod = Math.max(c.robotCycleS, reform);
    const placeS = c.gripS + moveS(c) + c.gripS;
    const lastPickS = Math.max(placeS + Math.max(moveS(c), c.crateChangeS), reform);
    const perCrateS = (picks - 1) * pickPeriod + lastPickS;
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
    this.products = [];       // on the belt, front (nearest the gate) first
    this.nextArrival = 0;
    this.backlog = 0;         // arrived upstream but no room on the belt yet
    // Phases: waiting (over the line) -> gripping -> toPlace -> releasing -> toPick.
    this.robot = { phase: "waiting", elapsed: 0, duration: 0, group: [], slots: [] };
    this.crate = { count: 0, changing: false, changeElapsed: 0, number: 1 };
    this.placeTimes = [];
    this.stats = { arrived: 0, packed: 0, picks: 0, crates: 0, busyS: 0, blockedS: 0 };
  };

  Simulation.prototype._arrivalGap = function () {
    const c = this.config;
    const mean = 60 / Math.max(c.infeedPpm, 1e-6);
    const min = (c.productLength * 1.05) / Math.max(c.beltSpeed, 1e-6);
    if (c.spacing !== "random" || mean <= min) return Math.max(mean, min);
    return min + -Math.log(1 - this.random()) * (mean - min);
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

  // Products pressed up in an unbroken line from the gate.
  Simulation.prototype.lineLength = function () {
    let n = 0;
    for (const p of this.products) {
      if (p.state !== "belt" || !p.settled) break;
      n += 1;
    }
    return n;
  };

  Simulation.prototype._tick = function (h, events) {
    const c = this.config;
    this.time += h;

    // Arrivals wait upstream until the belt start is clear.
    if (c.infeedPpm > 0) {
      while (this.nextArrival <= this.time) {
        this.backlog += 1;
        this.nextArrival += this._arrivalGap();
      }
    }
    const tail = this.products[this.products.length - 1];
    if (this.backlog > 0) {
      if (!tail || tail.state === "carried" || tail.x >= c.productLength * 1.05) {
        const product = { id: this.nextId++, x: 0, state: "belt", settled: false };
        this.products.push(product);
        this.backlog -= 1;
        this.stats.arrived += 1;
        events.push({ type: "spawn", id: product.id });
      } else if (tail.settled) {
        this.stats.blockedS += h;   // the line has backed up to the belt start
      }
    }

    // Belt: each product runs until it meets the gate or the product ahead.
    // A gripped product has not left the belt yet, so it still holds its place.
    let limit = c.gateX - c.productLength / 2;
    let aheadSettled = true;
    for (const p of this.products) {
      if (p.state !== "belt" && p.state !== "gripped") continue;
      if (p.state === "belt") {
        p.x = Math.min(p.x + c.beltSpeed * h, limit);
        p.settled = aheadSettled && p.x >= limit - 1e-6;
      }
      aheadSettled = p.settled;
      limit = p.x - c.productLength;
    }

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
    if (r.phase !== "waiting") {
      r.elapsed += h;
      this.stats.busyS += h;
    }
    if (r.phase === "waiting" && !this.crate.changing && c.perCrate > 0) {
      const want = Math.min(c.productsPerPick, c.perCrate - this.crate.count);
      if (want > 0 && this.lineLength() >= want) {
        r.group = this.products.filter((p) => p.state === "belt").slice(0, want);
        r.slots = r.group.map((_, i) => this.crate.count + i);
        for (const p of r.group) p.state = "gripped";
        this._phase("gripping", c.gripS);
        events.push({ type: "gripStart", ids: r.group.map((p) => p.id), slots: r.slots.slice() });
      }
    } else if (r.phase === "gripping" && r.elapsed >= r.duration) {
      for (const p of r.group) p.state = "carried";
      this._phase("toPlace", moveS(c));
      events.push({ type: "picked", ids: r.group.map((p) => p.id) });
    } else if (r.phase === "toPlace" && r.elapsed >= r.duration) {
      this._phase("releasing", c.gripS);
    } else if (r.phase === "releasing" && r.elapsed >= r.duration) {
      for (const p of r.group) {
        p.state = "placed";
        this.placeTimes.push(this.time);
      }
      this.crate.count += r.group.length;
      this.stats.packed += r.group.length;
      this.stats.picks += 1;
      events.push({ type: "placed", ids: r.group.map((p) => p.id), slots: r.slots.slice(), crate: this.crate.number });
      this.products = this.products.filter((p) => p.state !== "placed");
      r.group = [];
      r.slots = [];
      this._phase("toPick", moveS(c));
      if (this.crate.count >= c.perCrate) {
        this.crate.changing = true;
        this.crate.changeElapsed = 0;
        this.stats.crates += 1;
        events.push({ type: "crateFull", number: this.crate.number });
      }
    } else if (r.phase === "toPick" && r.elapsed >= r.duration) {
      this._phase("waiting", 0);
    }
  };

  Simulation.prototype._phase = function (phase, duration) {
    this.robot.phase = phase;
    this.robot.elapsed = 0;
    this.robot.duration = duration;
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

  return { packPattern, layoutPattern, stationPattern, estimateCapacityPpm, Simulation, DEFAULTS, mulberry32 };
});
