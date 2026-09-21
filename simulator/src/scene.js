// PikPak simulator page: the three.js scene and the side panel. Browser only.
// Everything it shows comes from the pure modules: PikPakEngine (flow and
// tray patterns), AuboI10 (arm pose) and PikPakSku (uploaded SKU files).
// Scene units are millimetres, y up; the belt runs along +x.
(function () {
  "use strict";
  const THREE = window.THREE, Engine = window.PikPakEngine, Arm = window.AuboI10, Sku = window.PikPakSku;

  // Cell layout: positions and sizes from the PikPak 2 general assembly
  // (QLT35-001-7303), reduced to plain blocks. x runs along the machine, z
  // across it from the centre of the product belt, y up from the floor.
  const CELL = {
    beltTop: 730, beltStart: -2370, beltEnd: 1830, beltWidth: 580,
    gateX: -175,            // where the products are held, level with the arm
    frame: { x0: -900, x1: 900, z0: -290, z1: 1805, height: 2140, plinth: 200 },
    robot: { x: -530, z: 605, pedestal: 690 },
    station: { x: 270, z: 675, floor: 613 },     // where a tray is filled
    trayIn: { z: 675, width: 700, x0: 880, x1: 3300, rise: 210 },   // sloping roller lane, trays run down it into the machine
    trayOut: { z: 1370, width: 700, x0: 920, x1: 3320, top: 402 },  // level lane the full trays leave on
    transfer: { x0: -34, x1: 1691, z0: 320, z1: 1717 },             // full trays are pushed across from the first lane to the second
    headDrop: 170,          // tool flange down to the cup lips
    hover: 130,             // how far above the products the head waits
    trayWall: 12,
  };
  const COLORS = { arm: 0xf08a24, joint: 0x2b2f36, belt: 0x30363d, frame: 0x9aa4af, gate: 0xd9482b,
    tray: 0x1f7a4d, cup: 0x1b1e23, punnet: 0xdfe7ea, floor: 0xe9edf0 };

  const $ = (id) => document.getElementById(id);
  const state = {
    skus: [], sku: null, pattern: null, sim: null, images: {}, speed: 1, running: true,
    meshes: new Map(), placed: [], lastPlace: null, armPose: null, textureCache: {},
  };

  // ---------- three.js basics ----------
  const canvas = $("view");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 50, 30000);
  const dark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
  function applyTheme() {
    scene.background = new THREE.Color(dark() ? 0x14191f : 0xf3f5f7);
    floor.material.color.set(dark() ? 0x1d242c : COLORS.floor);
  }

  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f98, 1.0));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(-900, 4200, -2200);
  sun.target.position.set(300, 500, 700);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -3800, right: 3800, top: 3800, bottom: -3800, near: 100, far: 10000 });
  scene.add(sun, sun.target);

  const floor = new THREE.Mesh(new THREE.PlaneGeometry(12000, 12000), new THREE.MeshStandardMaterial({ color: COLORS.floor, roughness: 1 }));
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);

  function box(w, h, d, color, opts) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshStandardMaterial(Object.assign({ color, roughness: 0.7 }, opts)));
    m.castShadow = true; m.receiveShadow = true;
    return m;
  }

  // Minimal orbit control: drag to turn, wheel to zoom, right-drag or shift-drag to pan.
  const orbit = { target: new THREE.Vector3(250, 800, 600), radius: 5600, theta: -2.5, phi: 1.02 };
  function placeCamera() {
    const s = Math.sin(orbit.phi);
    camera.position.set(
      orbit.target.x + orbit.radius * s * Math.sin(orbit.theta),
      orbit.target.y + orbit.radius * Math.cos(orbit.phi),
      orbit.target.z + orbit.radius * s * Math.cos(orbit.theta));
    camera.lookAt(orbit.target);
  }
  (function bindOrbit() {
    let drag = null;
    canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey }; canvas.setPointerCapture(e.pointerId); });
    canvas.addEventListener("pointerup", () => { drag = null; });
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
    canvas.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      if (drag.pan) {
        const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
        const k = orbit.radius / 900;
        orbit.target.addScaledVector(right, -dx * k);
        orbit.target.y = Math.max(0, orbit.target.y + dy * k);
      } else {
        orbit.theta -= dx * 0.006;
        orbit.phi = Math.min(1.5, Math.max(0.15, orbit.phi - dy * 0.006));
      }
      placeCamera();
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      orbit.radius = Math.min(9000, Math.max(1200, orbit.radius * (e.deltaY > 0 ? 1.1 : 0.9)));
      placeCamera();
    }, { passive: false });
  })();

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);

  // ---------- the cell ----------
  const cell = new THREE.Group();        // rebuilt per SKU: belt, gate, station
  const stationGroup = new THREE.Group(); // trays being filled and what is packed in them
  const leavingGroup = new THREE.Group(); // full trays on their way out
  const productLayer = new THREE.Group(); // products on the belt and in the head
  scene.add(cell, stationGroup, leavingGroup, productLayer);
  const cleats = [];

  function buildCell() {
    cell.clear();
    cleats.length = 0;
    const p = state.sku.product;
    const steel = { metalness: 0.5, roughness: 0.4 };
    const put = (mesh, x, y, z) => { mesh.position.set(x, y, z); cell.add(mesh); return mesh; };

    // Product infeed belt, right through the machine along one side.
    const length = CELL.beltEnd - CELL.beltStart, midX = (CELL.beltStart + CELL.beltEnd) / 2;
    put(box(length, 40, CELL.beltWidth - 60, COLORS.belt, { roughness: 0.9 }), midX, CELL.beltTop - 20, 0);
    put(box(length, 115, CELL.beltWidth, COLORS.frame, steel), midX, CELL.beltTop - 98, 0);
    for (const side of [-1, 1]) {
      put(box(length, 60, 30, COLORS.frame, steel), midX, CELL.beltTop + 30, side * (CELL.beltWidth / 2 - 15));
      for (const x of [CELL.beltStart + 150, CELL.beltStart + 1100, CELL.beltEnd - 150]) {
        put(box(50, CELL.beltTop - 155, 50, COLORS.frame, steel), x, (CELL.beltTop - 155) / 2, side * (CELL.beltWidth / 2 - 40));
      }
    }
    for (let i = 0; i < 24; i++) {
      const cleat = box(8, 3, CELL.beltWidth - 70, 0x4a525c);
      cleat.position.y = CELL.beltTop + 1;
      cell.add(cleat);
      cleats.push(cleat);
    }
    // The gate: a plate across the belt that the products run up against.
    const gateHeight = Math.max(p.height * 0.7, 40);
    put(box(16, gateHeight, CELL.beltWidth - 64, COLORS.gate, { metalness: 0.2 }), CELL.gateX + 8, CELL.beltTop + gateHeight / 2, 0);

    put(box(300, CELL.robot.pedestal, 300, COLORS.frame, steel), CELL.robot.x, CELL.robot.pedestal / 2, CELL.robot.z);

    // Tray infeed: a sloping roller lane, empty trays run down it into the machine.
    const tin = CELL.trayIn, inLength = tin.x1 - tin.x0;
    const slope = Math.atan2(tin.rise, inLength);
    const lane = put(box(inLength / Math.cos(slope), 50, tin.width, COLORS.frame, steel), (tin.x0 + tin.x1) / 2, CELL.station.floor - 25 + tin.rise / 2, tin.z);
    lane.rotation.z = slope;
    for (const x of [tin.x0 + 200, tin.x1 - 200]) {
      const h = CELL.station.floor - 60 + ((x - tin.x0) / inLength) * tin.rise;
      for (const dz of [-tin.width / 2 + 30, tin.width / 2 - 30]) put(box(45, h, 45, COLORS.frame, steel), x, h / 2, tin.z + dz);
    }
    // Tray outfeed: a level lane beside it, full trays leave along it.
    const tout = CELL.trayOut;
    put(box(tout.x1 - tout.x0, 105, tout.width, COLORS.frame, steel), (tout.x0 + tout.x1) / 2, tout.top - 52, tout.z);
    for (const x of [tout.x0 + 200, tout.x1 - 200]) {
      for (const dz of [-tout.width / 2 + 30, tout.width / 2 - 30]) put(box(45, tout.top - 105, 45, COLORS.frame, steel), x, (tout.top - 105) / 2, tout.z + dz);
    }
    // Cross transfer inside the machine: carries the tray being filled and pushes full ones across.
    const tr = CELL.transfer;
    put(box(tr.x1 - tr.x0, 160, tr.z1 - tr.z0, 0x7f8a96, steel), (tr.x0 + tr.x1) / 2, CELL.station.floor - 80, (tr.z0 + tr.z1) / 2);
    for (const x of [tr.x0 + 80, tr.x1 - 80]) {
      for (const z of [tr.z0 + 80, tr.z1 - 80]) put(box(60, CELL.station.floor - 160, 60, COLORS.frame, steel), x, (CELL.station.floor - 160) / 2, z);
    }

    // The machine frame: plinth and 40 mm posts and rails, left open so the inside shows.
    const f = CELL.frame, fw = f.x1 - f.x0, fd = f.z1 - f.z0, post = 40;
    for (const x of [f.x0, f.x1]) {
      for (const z of [f.z0, f.z1]) put(box(post, f.height - f.plinth, post, 0xc7ced6, steel), x, f.plinth + (f.height - f.plinth) / 2, z);
    }
    for (const z of [f.z0, f.z1]) {
      put(box(fw + post, post, post, 0xc7ced6, steel), (f.x0 + f.x1) / 2, f.height - post / 2, z);
      put(box(fw + post, f.plinth, 100, COLORS.frame, steel), (f.x0 + f.x1) / 2, f.plinth / 2, z);
    }
    for (const x of [f.x0, f.x1]) {
      put(box(post, post, fd + post, 0xc7ced6, steel), x, f.height - post / 2, (f.z0 + f.z1) / 2);
      put(box(100, f.plinth, fd, COLORS.frame, steel), x, f.plinth / 2, (f.z0 + f.z1) / 2);
    }
  }

  function trayMesh(tray) {
    const g = new THREE.Group(), t = CELL.trayWall;
    const mat = { roughness: 0.6 };
    const base = box(tray.length + 2 * t, t, tray.width + 2 * t, COLORS.tray, mat);
    base.position.y = t / 2;
    g.add(base);
    for (const s of [-1, 1]) {
      const long = box(tray.length + 2 * t, tray.depth, t, COLORS.tray, mat);
      long.position.set(0, t + tray.depth / 2, s * (tray.width / 2 + t / 2));
      const short = box(t, tray.depth, tray.width, COLORS.tray, mat);
      short.position.set(s * (tray.length / 2 + t / 2), t + tray.depth / 2, 0);
      g.add(long, short);
    }
    return g;
  }

  function fillStation() {
    stationGroup.clear();
    state.placed = [];
    stationGroup.position.set(CELL.station.x, CELL.station.floor, CELL.station.z);
    const n = state.pattern.trays;
    for (let k = 0; k < n; k++) {
      const tray = trayMesh(state.sku.tray);
      if (n === 2) {
        tray.rotation.y = Math.PI / 2;
        tray.position.x = (k - 0.5) * state.pattern.trayPitch;
      }
      stationGroup.add(tray);
    }
  }

  // ---------- products ----------
  function topTexture(sku) {
    const url = state.images[sku.id] || (/^(https?:|data:)/i.test(sku.image || "") ? sku.image : null);
    if (!url) return null;
    if (!state.textureCache[url]) {
      const tex = new THREE.TextureLoader().load(url);
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.anisotropy = 8;
      state.textureCache[url] = tex;
    }
    return state.textureCache[url];
  }

  function productMesh() {
    const size = state.pattern.slotSize, p = state.sku.product;
    const l = state.pattern.rotated ? size.width : size.length;
    const w = state.pattern.rotated ? size.length : size.width;
    const side = new THREE.MeshStandardMaterial({ color: COLORS.punnet, roughness: 0.35, transparent: true, opacity: 0.92 });
    const tex = topTexture(state.sku);
    const top = new THREE.MeshStandardMaterial(tex ? { map: tex, roughness: 0.5 } : { color: 0xf4f1e8, roughness: 0.6 });
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(Math.min(l, p.length), size.height, Math.min(w, p.width)), [side, side, top, side, side, side]);
    mesh.castShadow = true; mesh.receiveShadow = true;
    return mesh;
  }

  const slotWorld = (slot) => new THREE.Vector3(
    CELL.station.x + slot.x,
    CELL.station.floor + CELL.trayWall + slot.z + state.pattern.slotSize.height / 2,
    CELL.station.z + slot.y);
  // In the tray a product either keeps the way it lay on the belt or is turned 90 degrees.
  const slotTurn = (slot) => (slot.rotated ? Math.PI / 2 : 0);
  const beltWorld = (x) => new THREE.Vector3(CELL.beltStart + x, CELL.beltTop + state.pattern.slotSize.height / 2, 0);

  // ---------- the arm ----------
  const armGroup = new THREE.Group();
  scene.add(armGroup);
  const UP = new THREE.Vector3(0, 1, 0);
  function link(radius, color) {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, 1, 28),
      new THREE.MeshStandardMaterial({ color, roughness: 0.45, metalness: 0.1 }));
    m.castShadow = true;
    armGroup.add(m);
    return m;
  }
  function knuckle(radius) {
    const m = new THREE.Mesh(new THREE.SphereGeometry(radius * 1.06, 32, 20), new THREE.MeshStandardMaterial({ color: COLORS.joint, roughness: 0.5 }));
    m.castShadow = true;
    armGroup.add(m);
    return m;
  }
  const D = Arm.SPEC.approxDiameters;
  const LINKS = [
    ["base", "shoulder", link(D.baseJoint / 2, COLORS.joint)],
    ["shoulder", "upperArmStart", link(D.shoulderJoint / 2, COLORS.arm)],
    ["upperArmStart", "upperArmEnd", link(D.upperArmTube / 2, COLORS.arm)],
    ["upperArmEnd", "forearmStart", link(D.elbowJoint / 2, COLORS.arm)],
    ["forearmStart", "forearmEnd", link(D.forearmTube / 2, COLORS.arm)],
    ["forearmEnd", "wrist1", link(D.wristJoint / 2, COLORS.arm)],
    ["wrist1", "wrist2", link(D.wristJoint / 2, COLORS.arm)],
    ["wrist2", "flange", link(D.wristJoint / 2, COLORS.joint)],
  ];
  const KNUCKLES = [["shoulder", knuckle(D.shoulderJoint / 2)], ["upperArmStart", knuckle(D.shoulderJoint / 2)],
    ["upperArmEnd", knuckle(D.elbowJoint / 2)], ["forearmStart", knuckle(D.elbowJoint / 2)],
    ["forearmEnd", knuckle(D.wristJoint / 2)], ["wrist1", knuckle(D.wristJoint / 2)], ["wrist2", knuckle(D.wristJoint / 2)]];
  // Robot frame (z up) -> scene (y up), base on top of the pedestal.
  const armToWorld = (p) => new THREE.Vector3(CELL.robot.x + p.x, CELL.robot.pedestal + p.z, CELL.robot.z + p.y);

  function poseArm(flangeWorld, yaw) {
    const pose = Arm.solveToolDown({ x: flangeWorld.x - CELL.robot.x, y: flangeWorld.z - CELL.robot.z, z: flangeWorld.y - CELL.robot.pedestal }, yaw);
    if (pose.reachable) state.armPose = pose;
    if (!state.armPose) return;
    const pts = {};
    for (const key of Object.keys(state.armPose.points)) pts[key] = armToWorld(state.armPose.points[key]);
    for (const [a, b, mesh] of LINKS) {
      const dir = new THREE.Vector3().subVectors(pts[b], pts[a]);
      const len = dir.length();
      mesh.position.copy(pts[a]).addScaledVector(dir, 0.5);
      mesh.scale.set(1, Math.max(len, 1), 1);
      mesh.quaternion.setFromUnitVectors(UP, dir.normalize());
    }
    for (const [key, mesh] of KNUCKLES) mesh.position.copy(pts[key]);
    $("reach").hidden = pose.reachable;
  }

  // The vacuum head: a bar under the flange with one cup per product.
  const head = new THREE.Group();
  scene.add(head);
  let cups = [];
  function buildHead() {
    head.clear();
    cups = [];
    const stem = box(60, CELL.headDrop - 60, 60, COLORS.joint);
    stem.position.y = -(CELL.headDrop - 60) / 2;
    const bar = box(100, 30, 70, COLORS.frame, { metalness: 0.6, roughness: 0.35 });
    bar.name = "bar";
    bar.position.y = -(CELL.headDrop - 45);
    head.add(stem, bar);
    for (let i = 0; i < state.sim.config.productsPerPick; i++) {
      const cup = new THREE.Mesh(new THREE.CylinderGeometry(22, 30, 30, 20), new THREE.MeshStandardMaterial({ color: COLORS.cup, roughness: 0.8 }));
      cup.castShadow = true;
      head.add(cup);
      cups.push(cup);
    }
  }

  // ---------- one SKU ----------
  function chooseSku(sku) {
    state.sku = sku;
    state.pattern = Engine.stationPattern(sku.tray, sku.product, Object.assign({}, sku, { squeeze: Sku.SQUEEZE_MM }));
    if (sku.infeedPpm) $("infeed").value = sku.infeedPpm;
    restart();
  }

  function restart() {
    const sku = state.sku;
    state.sim = new Engine.Simulation({
      infeedPpm: +$("infeed").value, spacing: $("spacing").value, beltSpeed: +$("belt").value,
      gateX: CELL.gateX - CELL.beltStart, productLength: sku.product.length, productsPerPick: sku.productsPerPick,
      robotCycleS: +$("cycle").value, crateChangeS: +$("change").value, perCrate: state.pattern.total, seed: 1,
    });
    for (const mesh of state.meshes.values()) productLayer.remove(mesh);
    state.meshes.clear();
    state.lastPlace = null;
    buildCell();
    leavingGroup.clear();
    fillStation();
    buildHead();
    describeSku();
  }

  function describeSku() {
    const s = state.sku, p = state.pattern;
    const kg = (s.weightG || 0) * s.productsPerPick / 1000;
    $("skuInfo").innerHTML = "";
    const rows = [
      ["Product", `${s.product.length} x ${s.product.width} x ${s.product.height} mm` + (s.weightG ? `, ${s.weightG} g` : "")],
      ["Tray", `${s.tray.name || "Tray"}: ${s.tray.length} x ${s.tray.width} x ${s.tray.depth} mm inside` + (p.trays === 2 ? ", two side by side" : "")],
      ["Layout", `${s.rows} x ${s.columns} per layer, ${s.layers} layer${s.layers > 1 ? "s" : ""}: ${p.perTray} per tray` + (p.rotated ? ", turned 90 degrees" : "")],
      ["Room to spare", `x ${p.spare.x} mm, y ${p.spare.y} mm, z ${p.spare.z} mm` + (p.tight ? " (tight fit, inside the squeeze allowance)" : "")],
      ["Each lift", `${s.productsPerPick} products` + (s.weightG ? `, ${kg.toFixed(2)} kg of the arm's ${Arm.SPEC.payloadKg} kg` : "")],
    ];
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k; dd.textContent = v;
      $("skuInfo").append(dt, dd);
    }
  }

  // ---------- frame loop ----------
  const smooth = (t) => t * t * (3 - 2 * t);
  // From a to b around the robot: the angle and the distance from its base are
  // blended, so a move from the belt to the trays swings past the arm instead
  // of cutting straight through its column.
  function swing(a, b, s) {
    const ax = a.x - CELL.robot.x, az = a.z - CELL.robot.z, bx = b.x - CELL.robot.x, bz = b.z - CELL.robot.z;
    const angle = Math.atan2(az, ax) + (Math.atan2(bz, bx) - Math.atan2(az, ax)) * s;
    const radius = Math.hypot(ax, az) + (Math.hypot(bx, bz) - Math.hypot(ax, az)) * s;
    return new THREE.Vector3(CELL.robot.x + radius * Math.cos(angle), a.y + (b.y - a.y) * s, CELL.robot.z + radius * Math.sin(angle));
  }
  const centroid = (points) => points.reduce((sum, q) => sum.add(q), new THREE.Vector3()).multiplyScalar(1 / points.length);
  function groupPickPositions(group) { return group.map((p) => beltWorld(p.x)); }

  function animate(sim) {
    const r = sim.robot, n = sim.config.productsPerPick, L = sim.config.productLength;
    const contactY = CELL.beltTop + state.pattern.slotSize.height + CELL.headDrop;
    const waitCentre = new THREE.Vector3(CELL.gateX - (n * L) / 2, contactY + CELL.hover, 0);
    const t = r.duration > 0 ? Math.min(1, r.elapsed / r.duration) : 1;
    let flange = waitCentre.clone(), yaw = 0, cupTargets = null;

    if (r.phase === "gripping") {
      flange.y = contactY + CELL.hover * (1 - smooth(t));
    } else if (r.phase === "toPlace" || r.phase === "releasing") {
      const s = r.phase === "releasing" ? 1 : smooth(t);
      const from = groupPickPositions(r.group);
      const to = r.slots.map((i) => slotWorld(state.pattern.stationSlots[i]));
      const lift = Math.sin(Math.PI * s) * 260;
      cupTargets = [];
      const fromCentre = centroid(from.map((q) => q.clone())), toCentre = centroid(to.map((q) => q.clone()));
      const centre = swing(fromCentre, toCentre, s);
      centre.y += lift;
      r.group.forEach((p, i) => {
        // Each product keeps its place in the line while the line closes up to the tray pitch.
        const offset = from[i].clone().sub(fromCentre).lerp(to[i].clone().sub(toCentre), s);
        const pos = centre.clone().add(offset);
        const mesh = state.meshes.get(p.id);
        if (mesh) { mesh.position.copy(pos); mesh.rotation.y = slotTurn(state.pattern.stationSlots[r.slots[i]]) * s; }
        cupTargets.push(pos);
      });
      flange = centre.clone();
      flange.y += state.pattern.slotSize.height / 2 + CELL.headDrop;
      yaw = (state.pattern.line === "y" ? Math.PI / 2 : 0) * s;
      state.lastPlace = { flange: flange.clone(), yaw };
    } else if (r.phase === "toPick" && state.lastPlace) {
      const s = smooth(t);
      flange = swing(state.lastPlace.flange, waitCentre, s);
      flange.y += Math.sin(Math.PI * s) * 200;
      yaw = state.lastPlace.yaw * (1 - s);
    }

    poseArm(flange, yaw);
    head.position.copy(flange);
    head.rotation.y = -yaw;
    // Cups sit over each product; with nothing held they wait at the belt pitch.
    let minX = Infinity, maxX = -Infinity;
    cups.forEach((cup, i) => {
      let local;
      if (cupTargets && cupTargets[i]) {
        local = head.worldToLocal(cupTargets[i].clone());
        local.y = -CELL.headDrop + 15;
      } else {
        local = new THREE.Vector3((i - (cups.length - 1) / 2) * L, -CELL.headDrop + 15, 0);
      }
      cup.visible = !cupTargets || !!cupTargets[i];
      cup.position.copy(local);
      if (cup.visible) { minX = Math.min(minX, local.x); maxX = Math.max(maxX, local.x); }
    });
    const bar = head.getObjectByName("bar");
    bar.scale.x = Math.max(1, (maxX - minX + 90) / 100);
    bar.position.x = (minX + maxX) / 2;
    bar.position.z = 0;
  }

  function handle(events) {
    for (const e of events) {
      if (e.type === "spawn") {
        const mesh = productMesh();
        state.meshes.set(e.id, mesh);
        productLayer.add(mesh);
      } else if (e.type === "placed") {
        e.ids.forEach((id, i) => {
          const mesh = state.meshes.get(id);
          if (!mesh) return;
          const slot = state.pattern.stationSlots[e.slots[i]];
          state.meshes.delete(id);
          productLayer.remove(mesh);
          mesh.position.copy(slotWorld(slot)).sub(new THREE.Vector3(CELL.station.x, CELL.station.floor, CELL.station.z));
          mesh.rotation.y = slotTurn(slot);
          stationGroup.add(mesh);
        });
      } else if (e.type === "crateFull") {
        leavingGroup.clear();
        for (const child of [...stationGroup.children]) leavingGroup.add(child);
        fillStation();
      } else if (e.type === "crateIn") {
        leavingGroup.clear();
      }
    }
  }

  // Move the simulation on by dt seconds and draw it.
  function advance(dt) {
    const sim = state.sim;
    if (dt > 0) handle(sim.step(dt));
    for (const p of sim.products) {
      const mesh = state.meshes.get(p.id);
      if (mesh && (p.state === "belt" || p.state === "gripped")) { mesh.position.copy(beltWorld(p.x)); mesh.rotation.y = 0; }
    }
    // Tray change: the full tray is pushed across to the outfeed lane and runs out
    // along it; the next empty tray runs down the sloping infeed lane to the arm.
    const st = CELL.station;
    if (sim.crate.changing) {
      const k = Math.min(1, sim.crate.changeElapsed / sim.config.crateChangeS);
      const across = smooth(Math.min(1, k / 0.35)), out = smooth(Math.max(0, (k - 0.35) / 0.65));
      leavingGroup.position.set(st.x + out * (CELL.trayOut.x1 - st.x + 500), st.floor + across * (CELL.trayOut.top - st.floor), st.z + across * (CELL.trayOut.z - st.z));
      const arrive = 1 - smooth(Math.max(0, (k - 0.2) / 0.8));
      const run = arrive * (CELL.trayIn.x1 - st.x - 400);
      stationGroup.position.set(st.x + run, st.floor + (run / (CELL.trayIn.x1 - CELL.trayIn.x0)) * CELL.trayIn.rise, st.z);
      stationGroup.rotation.z = arrive > 0.02 ? Math.atan2(CELL.trayIn.rise, CELL.trayIn.x1 - CELL.trayIn.x0) : 0;
    } else {
      stationGroup.position.set(st.x, st.floor, st.z);
      stationGroup.rotation.z = 0;
    }
    const span = CELL.beltEnd - CELL.beltStart;
    cleats.forEach((cleat, i) => {
      cleat.position.x = CELL.beltStart + ((sim.time * sim.config.beltSpeed + (i * span) / cleats.length) % span);
    });
    animate(sim);
    renderer.render(scene, camera);
  }

  let last = performance.now(), statsAt = 0;
  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min(0.1, (now - last) / 1000) * (state.running ? state.speed : 0);
    last = now;
    if (!state.sim) return;
    advance(dt);
    if (now - statsAt > 200) { statsAt = now; showStats(state.sim); }
  }

  function showStats(sim) {
    const capacity = Engine.estimateCapacityPpm(sim.config);
    const set = (id, text) => { $(id).textContent = text; };
    set("sPacked", sim.stats.packed);
    set("sTrays", sim.stats.crates * state.pattern.trays);
    set("sPpm", sim.rollingPpm().toFixed(0));
    set("sCapacity", capacity.toFixed(0));
    set("sBusy", Math.round(sim.utilisation() * 100) + "%");
    set("sBlocked", sim.stats.blockedS.toFixed(0) + " s");
    set("sTime", sim.time.toFixed(0) + " s");
    const over = sim.config.infeedPpm > capacity + 0.5;
    $("verdict").textContent = over
      ? `The line brings ${sim.config.infeedPpm} a minute; this setup packs about ${capacity.toFixed(0)}. The belt backs up.`
      : `Keeps up: ${sim.config.infeedPpm} a minute arriving, room for about ${capacity.toFixed(0)}.`;
    $("verdict").className = over ? "verdict over" : "verdict ok";
  }

  // ---------- panel ----------
  function listSkus() {
    const select = $("sku");
    select.innerHTML = "";
    state.skus.forEach((s, i) => {
      const o = document.createElement("option");
      o.value = i; o.textContent = s.name + (s.tray.name ? ` - ${s.tray.name}` : "");
      select.append(o);
    });
  }
  $("sku").addEventListener("change", () => chooseSku(state.skus[+$("sku").value]));
  for (const id of ["infeed", "belt", "cycle", "change", "spacing"]) {
    $(id).addEventListener("input", () => { showValues(); restart(); });
  }
  $("speed").addEventListener("input", () => { state.speed = +$("speed").value; showValues(); });
  $("pause").addEventListener("click", () => { state.running = !state.running; $("pause").textContent = state.running ? "Pause" : "Play"; });
  $("reset").addEventListener("click", restart);
  function showValues() {
    $("infeedV").textContent = $("infeed").value + " / min";
    $("beltV").textContent = $("belt").value + " mm/s";
    $("cycleV").textContent = (+$("cycle").value).toFixed(2) + " s";
    $("changeV").textContent = (+$("change").value).toFixed(1) + " s";
    $("speedV").textContent = $("speed").value + "x";
  }

  // Upload: the SKU file(s) and the product pictures, all in one go.
  $("files").addEventListener("change", async (event) => {
    const files = [...event.target.files];
    const pictures = files.filter((f) => /^image\//.test(f.type));
    const sheets = files.filter((f) => /\.(csv|json|txt)$/i.test(f.name));
    const skus = [], problems = [];
    for (const file of sheets) {
      const result = Sku.parseSkuFile(await file.text(), file.name);
      skus.push(...result.skus);
      problems.push(...result.problems.map((p) => Object.assign({ file: file.name }, p)));
    }
    if (sheets.length) {
      const matched = Sku.matchImages(skus, pictures.map((f) => f.name));
      for (const id of Object.keys(matched.matches)) {
        state.images[id] = URL.createObjectURL(pictures.find((f) => f.name === matched.matches[id]));
      }
      for (const id of matched.missing) problems.push({ level: "warning", sku: id.split("@")[0], message: "its picture was not among the uploaded files" });
    } else if (pictures.length && state.sku) {
      // Pictures on their own: the first one goes on the product showing now.
      state.images[state.sku.id] = URL.createObjectURL(pictures[0]);
    }
    const list = $("problems");
    list.innerHTML = "";
    for (const p of problems.filter((q) => q.level === "error" || !/no image|no weight/.test(q.message))) {
      const li = document.createElement("li");
      li.className = p.level;
      li.textContent = `${p.sku}: ${p.message}`;
      list.append(li);
    }
    if (skus.length) {
      state.skus = skus;
      listSkus();
      chooseSku(skus[0]);
    } else if (state.sku) restart();
    $("uploadNote").textContent = sheets.length ? `${skus.length} product${skus.length === 1 ? "" : "s"} loaded` + (problems.some((p) => p.level === "error") ? ", some refused:" : "") : "";
    event.target.value = "";
  });

  // ---------- start ----------
  const builtIn = Sku.parseSkuFile($("builtInSkus").textContent, "built-in.csv");
  state.skus = builtIn.skus;
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  applyTheme();
  placeCamera();
  resize();
  showValues();
  listSkus();
  chooseSku(state.skus[0]);
  window.pikpak = Object.assign(state, { head, orbit, placeCamera, CELL, advance, showStats });   // for poking at from the console
  requestAnimationFrame(frame);
})();
