// PikPak simulator page: the three.js scene and the side panel. Browser only.
// Everything it shows comes from the pure modules: PikPakEngine (flow and
// tray patterns), AuboI10 (arm pose) and PikPakSku (uploaded SKU files).
// Scene units are millimetres, y up; the belt runs along +x.
(function () {
  "use strict";
  const THREE = window.THREE, Engine = window.PikPakEngine, Arm = window.AuboI10, Sku = window.PikPakSku;

  // Cell layout: every position and size below is read out of the PikPak 2
  // general assembly (QLT35-001-7303) and reduced to plain blocks. Scene x
  // runs along the machine in the direction the products travel (CAD +Z), y
  // is up (CAD +Y), and z = -(CAD X + 2755): across the machine from the
  // centre of the product belt, the tray lanes on the negative side. That is
  // a turn of the CAD, not a mirror image of it. CAD names are in the comments.
  const CELL = {
    // INFEED CONVEYOR, 4.2m LONG, 405mm WIDE, 48m/min
    belt: { x0: -2370, x1: 1830, top: 730, width: 405, bodyWidth: 580, bodyDepth: 156 },
    // Not in the CAD. Chris, 2026-09-22: about 1 m further from the robot along Z than the
    // first guess (-90); then back by about 200 mm: CAD Z 700, inside the outfeed side of the frame.
    gateX: 700,
    // WELDMENT FRAME
    frame: { x0: -901, x1: 900, z0: -1805, z1: 305, height: 2140, plinth: 200 },
    // ROBOT, iS SERIES on WELDMENT, ROBOT PEDESTAL
    robot: { x: -441, z: -468, pedestal: 690, pedestalX: 185, pedestalZ: 360 },
    // CONVEYOR WITH RIVETS (internal tray belt, 600 wide) up to the ejector arm's END STOP GUIDE
    trayBelt: { x0: -316, x1: 885, z: -670, width: 818, top: 471, endStop: -275 },
    station: { x: -75, z: -670, floor: 471 },     // a 400 deep tray hard against the end stop
    // POWERED ROLLERS ASSY, 1.2m SECTION (internal outfeed lane)
    rollers: { x0: -315, x1: 889, z: -1368, width: 686, top: 466 },
    // EXTERNAL INFEED ROLLERS: gravity lane, empty trays run down it into the machine
    trayIn: { x0: 890, x1: 3290, z: -672, width: 686, topNear: 477, topFar: 655 },
    // EXTERNAL OUTFEED ROLLERS: gravity lane with an END STOP, full trays run out along it
    trayOut: { x0: 921, x1: 3321, z: -1368, width: 686, topNear: 402, topFar: 300 },
    controlBox: { x: 64, z: 24, y: 467, size: [441, 176, 351] },    // ROBOT CONTROL BOX, under the belt
    camera: { x: -465, z: -52, y: 1590, size: [195, 63, 60] },        // CAMERA ENCLOSURE over the pick
    hmi: { x: -1096, z: 223, y: 1275, size: [270, 217, 40] },       // HMI TABLET CASE
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
  sun.position.set(-900, 4200, 2200);
  sun.target.position.set(300, 500, -700);
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
  const orbit = { target: new THREE.Vector3(450, 600, -650), radius: 8600, theta: -0.42, phi: 0.98 };
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
      orbit.radius = Math.min(14000, Math.max(1200, orbit.radius * (e.deltaY > 0 ? 1.1 : 0.9)));
      placeCamera();
    }, { passive: false });
  })();

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    // Centre the machine in the space beside the panel, not behind it.
    const panel = w > 760 ? 356 : 0;
    if (panel) camera.setViewOffset(w + panel, h, 0, 0, w, h); else camera.clearViewOffset();
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);

  // ---------- axes ----------
  // The STEP file's own axes, drawn at its origin (on the floor, beyond the tray
  // lanes), so a position read off the CAD can be found in the scene and back.
  const axes = new THREE.Group();
  scene.add(axes);
  function label(text, color) {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 128;
    const g = c.getContext("2d");
    g.font = "600 84px Segoe UI, Arial, sans-serif";
    g.textAlign = "center"; g.textBaseline = "middle";
    g.fillStyle = color;
    g.fillText(text, 128, 66);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false }));
    sprite.scale.set(360, 180, 1);
    return sprite;
  }
  (function buildAxes() {
    const origin = new THREE.Vector3(0, 0, -2755);   // CAD (0, 0, 0)
    const each = [["X", new THREE.Vector3(0, 0, -1), "#e5484d"], ["Y", new THREE.Vector3(0, 1, 0), "#30a46c"], ["Z", new THREE.Vector3(1, 0, 0), "#3b82f6"]];
    for (const [name, dir, color] of each) {
      axes.add(new THREE.ArrowHelper(dir, origin, 800, color, 160, 90));
      const text = label(name, color);
      text.position.copy(origin).addScaledVector(dir, 980);
      axes.add(text);
    }
    const zero = label("0", "#8592a0");
    zero.position.copy(origin).add(new THREE.Vector3(-150, 60, 150));
    zero.scale.set(240, 120, 1);
    axes.add(zero);
  })();

  // ---------- the cell ----------
  const cell = new THREE.Group();        // rebuilt per SKU: belt, gate, station
  const trayLayer = new THREE.Group();    // every tray unit: waiting empty, being filled, leaving full
  const productLayer = new THREE.Group(); // products on the belt and in the head
  scene.add(cell, trayLayer, productLayer);
  const cleats = [];

  function buildCell() {
    cell.clear();
    cleats.length = 0;
    const p = state.sku.product;
    const steel = { metalness: 0.5, roughness: 0.4 };
    const put = (mesh, x, y, z) => { mesh.position.set(x, y, z); cell.add(mesh); return mesh; };
    const legs = (xs, zs, top) => { for (const x of xs) for (const z of zs) put(box(45, top, 45, COLORS.frame, steel), x, top / 2, z); };
    // A roller lane that may slope: a slab from (x0, top0) to (x1, top1).
    const lane = (x0, x1, z, width, top0, top1, depth, color) => {
      const run = x1 - x0, angle = Math.atan2(top1 - top0, run);
      const slab = put(box(Math.hypot(run, top1 - top0), depth, width, color, steel), (x0 + x1) / 2, (top0 + top1) / 2 - depth / 2, z);
      slab.rotation.z = angle;
      for (let i = 0; i < Math.floor(run / 150); i++) {   // rollers
        const x = x0 + 75 + i * 150, y = top0 + ((x - x0) / run) * (top1 - top0);
        const roller = new THREE.Mesh(new THREE.CylinderGeometry(25, 25, width - 60, 16), new THREE.MeshStandardMaterial({ color: 0xd5dbe1, metalness: 0.7, roughness: 0.3 }));
        roller.rotation.x = Math.PI / 2;
        put(roller, x, y - 25, z);
      }
    };

    // Product infeed conveyor, right through the machine along one side.
    const b = CELL.belt, length = b.x1 - b.x0, midX = (b.x0 + b.x1) / 2;
    put(box(length, b.bodyDepth, b.bodyWidth, COLORS.frame, steel), midX, b.top - 8 - b.bodyDepth / 2, 0);
    put(box(length, 10, b.width, COLORS.belt, { roughness: 0.9 }), midX, b.top - 5, 0);
    for (const side of [-1, 1]) put(box(3525, 30, 30, COLORS.frame, steel), -302, b.top - 41, side * (b.width / 2 + 27));
    legs([b.x0 + 200, b.x0 + 1250, b.x1 - 200], [-b.bodyWidth / 2 + 50, b.bodyWidth / 2 - 50], b.top - b.bodyDepth);
    for (let i = 0; i < 24; i++) {
      const cleat = box(8, 3, b.width - 10, 0x4a525c);
      cleat.position.y = b.top + 1;
      cell.add(cleat);
      cleats.push(cleat);
    }
    // The gate: a plate across the belt that the products run up against.
    const gateHeight = Math.max(p.height * 0.7, 40);
    put(box(16, gateHeight, b.width, COLORS.gate, { metalness: 0.2 }), CELL.gateX + 8, b.top + gateHeight / 2, 0);

    const r = CELL.robot;
    put(box(r.pedestalX, r.pedestal - 15, r.pedestalZ, COLORS.frame, steel), r.x + 18, (r.pedestal - 15) / 2, r.z);
    put(box(210, 15, 210, COLORS.joint), r.x, r.pedestal - 7, r.z);

    // Inside the machine: the tray belt the tray is filled on, its end stop, and the powered rollers beside it.
    const tb = CELL.trayBelt;
    put(box(tb.x1 - tb.x0, 185, tb.width, COLORS.frame, steel), (tb.x0 + tb.x1) / 2, tb.top - 10 - 92, tb.z);
    put(box(1047, 10, 600, COLORS.belt, { roughness: 0.9 }), (tb.x0 + tb.x1) / 2, tb.top - 5, tb.z);
    put(box(8, 90, 710, 0xd5dbe1, steel), tb.endStop - 4, 687, tb.z);   // END STOP GUIDE
    put(box(60, 120, 834, 0x5b6673, steel), tb.endStop - 60, tb.top + 40, tb.z - 25);   // EJECTOR ARM's cylinder, across the lane
    legs([tb.x0 + 60, tb.x1 - 60], [tb.z - 300, tb.z + 300], tb.top - 195);
    const ro = CELL.rollers;
    lane(ro.x0, ro.x1, ro.z, ro.width, ro.top, ro.top, 170, COLORS.frame);
    legs([ro.x0 + 60, ro.x1 - 60], [ro.z - 300, ro.z + 300], ro.top - 170);

    // Outside: the two gravity roller lanes, side by side.
    const tin = CELL.trayIn, tout = CELL.trayOut;
    lane(tin.x0, tin.x1, tin.z, tin.width, tin.topNear, tin.topFar, 105, COLORS.frame);
    legs([tin.x0 + 250], [tin.z - 300, tin.z + 300], tin.topNear - 90);
    legs([tin.x1 - 200], [tin.z - 300, tin.z + 300], tin.topFar - 110);
    lane(tout.x0, tout.x1, tout.z, tout.width, tout.topNear, tout.topFar, 105, COLORS.frame);
    legs([tout.x0 + 250], [tout.z - 300, tout.z + 300], tout.topNear - 110);
    legs([tout.x1 - 200], [tout.z - 300, tout.z + 300], tout.topFar - 100);
    put(box(40, 155, tout.width, 0xd5dbe1, steel), tout.x1 - 20, tout.topFar + 60, tout.z);   // END STOP

    const cb = CELL.controlBox, cam = CELL.camera, hmi = CELL.hmi;
    put(box(cb.size[0], cb.size[1], cb.size[2], 0x3a424c), cb.x, cb.y, cb.z);
    put(box(cam.size[0], cam.size[1], cam.size[2], 0x3a424c), cam.x, cam.y, cam.z);
    put(box(30, CELL.frame.height - cam.y - 60, 30, 0xc7ced6, steel), cam.x, (CELL.frame.height + cam.y) / 2, cam.z);
    put(box(hmi.size[0], hmi.size[1], hmi.size[2], 0x20262d), hmi.x, hmi.y, hmi.z);

    // The machine frame: plinth, 40 mm posts and rails, left open so the inside shows.
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

  // A tray unit is what moves through the machine as one: a full-size tray, or
  // two half-size trays side by side. Its origin is the centre of its floor.
  function trayUnit(x) {
    const unit = new THREE.Group();
    const n = state.pattern.trays;
    for (let k = 0; k < n; k++) {
      const tray = trayMesh(state.sku.tray);
      // Either way the unit is 600 across the lane and 400 along it.
      if (n === 2) tray.position.z = -(k - 0.5) * state.pattern.trayPitch;
      else tray.rotation.y = Math.PI / 2;
      unit.add(tray);
    }
    unit.position.set(x, laneTop(CELL.trayIn, CELL.trayBelt.top, x), CELL.station.z);
    trayLayer.add(unit);
    return unit;
  }
  // Height of the lane surface under x: level inside the machine, sloping outside it.
  function laneTop(outside, insideTop, x) {
    if (x <= outside.x0) return insideTop;
    return outside.topNear + ((x - outside.x0) / (outside.x1 - outside.x0)) * (outside.topFar - outside.topNear);
  }
  const UNIT_PITCH = 470;       // centre to centre of trays queued on a lane
  const WAIT_X = 1160;          // the first empty tray waits here, just outside the machine

  function fillStation() {
    trayLayer.clear();
    state.station = trayUnit(CELL.station.x);
    state.waiting = [0, 1, 2].map((i) => trayUnit(WAIT_X + i * UNIT_PITCH));
    state.leaving = [];
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

  // The station's long side (600) lies across the lane, so the pattern's x runs along scene z.
  const slotLocal = (slot) => new THREE.Vector3(slot.y, CELL.trayWall + slot.z + state.pattern.slotSize.height / 2, -slot.x);
  const slotWorld = (slot) => slotLocal(slot).add(new THREE.Vector3(CELL.station.x, CELL.station.floor, CELL.station.z));
  // On the belt a product's length runs along x; in the tray it lies along the
  // pattern's x (scene z) unless the pattern turned it.
  const slotTurn = (slot) => (slot.rotated ? 0 : Math.PI / 2);
  const beltWorld = (x) => new THREE.Vector3(CELL.belt.x0 + x, CELL.belt.top + state.pattern.slotSize.height / 2, 0);

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
  const armToWorld = (p) => new THREE.Vector3(CELL.robot.x + p.x, CELL.robot.pedestal + p.z, CELL.robot.z - p.y);

  function poseArm(flangeWorld, yaw) {
    const pose = Arm.solveToolDown({ x: flangeWorld.x - CELL.robot.x, y: CELL.robot.z - flangeWorld.z, z: flangeWorld.y - CELL.robot.pedestal }, yaw);
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
      gateX: CELL.gateX - CELL.belt.x0, productLength: sku.product.length, productsPerPick: sku.productsPerPick,
      robotCycleS: +$("cycle").value, crateChangeS: +$("change").value, perCrate: state.pattern.total, seed: 1,
    });
    for (const mesh of state.meshes.values()) productLayer.remove(mesh);
    state.meshes.clear();
    state.lastPlace = null;
    buildCell();
    fillStation();
    buildHead();
    describeSku();
    drawPlan();
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

  // ---------- tray plan ----------
  // The tray (or the two half trays side by side) from above, drawn from the
  // same slots the arm fills. Each product carries the number of the lift that
  // packs it; the layer being packed fills in as the arm works.
  const SVG = "http://www.w3.org/2000/svg";
  const el = (name, attrs, parent) => {
    const node = document.createElementNS(SVG, name);
    for (const key of Object.keys(attrs)) node.setAttribute(key, attrs[key]);
    parent.append(node);
    return node;
  };
  let planSlots = [];
  function drawPlan() {
    const svg = $("plan"), pat = state.pattern, tray = state.sku.tray, wall = CELL.trayWall;
    svg.innerHTML = "";
    planSlots = [];
    const pair = pat.trays === 2;
    // Station x runs along the 600: a half tray lies with its width along it.
    const trayX = pair ? tray.width : tray.length, trayY = pair ? tray.length : tray.width;
    const spanX = pair ? pat.trayPitch + trayX : trayX;
    const pad = wall + 14;
    svg.setAttribute("viewBox", `${-spanX / 2 - pad} ${-trayY / 2 - pad} ${spanX + 2 * pad} ${trayY + 2 * pad}`);
    for (let k = 0; k < pat.trays; k++) {
      const cx = pair ? (k - 0.5) * pat.trayPitch : 0;
      el("rect", { class: "tray", x: cx - trayX / 2 - wall / 2, y: -trayY / 2 - wall / 2, width: trayX + wall, height: trayY + wall, rx: 18 }, svg);
    }
    const sizeX = pair ? pat.slotSize.width : pat.slotSize.length, sizeY = pair ? pat.slotSize.length : pat.slotSize.width;
    const picture = state.images[state.sku.id] || (/^(https?:|data:)/i.test(state.sku.image || "") ? state.sku.image : null);
    const perPick = state.sim.config.productsPerPick;
    for (const slot of pat.stationSlots.filter((q) => q.layer === 0)) {
      const g = el("g", {}, svg);
      const rect = el("rect", { class: "slot", x: slot.x - sizeX / 2 + 3, y: slot.y - sizeY / 2 + 3, width: sizeX - 6, height: sizeY - 6, rx: 10 }, g);
      let image = null;
      if (picture) {
        // The picture is of the product lying lengthways; turn it where the product is turned.
        const long = Math.max(sizeX, sizeY) - 6, short = Math.min(sizeX, sizeY) - 6;
        const lengthAlongY = sizeY > sizeX;
        image = el("image", { href: picture, x: slot.x - long / 2, y: slot.y - short / 2, width: long, height: short, preserveAspectRatio: "none",
          transform: lengthAlongY ? `rotate(90 ${slot.x} ${slot.y})` : "" }, g);
      }
      const text = el("text", { x: slot.x, y: slot.y }, g);
      planSlots.push({ rect, image, text, place: slot.index, perPick });
    }
    $("planLayers").textContent = pat.layers;
    $("planTotal").textContent = pat.perTray;
    $("planPair").hidden = !pair;
    $("planPair").textContent = pair ? `Two half-size trays side by side, packed together: ${pat.total} products each time.` : "";
    updatePlan();
  }
  function updatePlan() {
    if (!state.sim || !planSlots.length) return;
    const pat = state.pattern, perLayer = pat.perLayer * pat.trays;
    const count = state.sim.crate.changing ? pat.total : state.sim.crate.count;
    const layer = Math.min(pat.layers - 1, Math.floor(count / perLayer));
    for (const q of planSlots) {
      const index = layer * perLayer + q.place;
      const packed = index < count;
      q.rect.setAttribute("class", "slot" + (packed ? " packed" : layer > 0 ? " below" : ""));
      if (q.image) q.image.style.display = packed ? "" : "none";
      q.text.textContent = Math.floor(index / q.perPick) + 1;
      q.text.style.opacity = packed && q.image ? 0 : 1;
    }
    $("planNote").textContent = state.sim.crate.changing ? "Tray full: changing trays"
      : `Packing layer ${layer + 1} of ${pat.layers}. Numbers are the lift that packs each product.`;
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
    const contactY = CELL.belt.top + state.pattern.slotSize.height + CELL.headDrop;
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
      yaw = (state.pattern.line === "y" ? 0 : Math.PI / 2) * s;
      state.lastPlace = { flange: flange.clone(), yaw };
    } else if (r.phase === "toPick" && state.lastPlace) {
      const s = smooth(t);
      flange = swing(state.lastPlace.flange, waitCentre, s);
      flange.y += Math.sin(Math.PI * s) * 200;
      yaw = state.lastPlace.yaw * (1 - s);
    }

    poseArm(flange, yaw);
    head.position.copy(flange);
    head.rotation.y = yaw;
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
          mesh.position.copy(slotLocal(slot));
          mesh.rotation.y = slotTurn(slot);
          state.station.add(mesh);
        });
      } else if (e.type === "crateFull") {
        // The full tray leaves, the first waiting tray comes in, another joins the queue outside.
        state.leaving.push({ unit: state.station, since: state.sim.time, parkedAt: null });
        state.station = state.waiting.shift();
        state.station.userData.startX = state.station.position.x;
        state.waiting.push(trayUnit(CELL.trayIn.x1 - 250));
      }
    }
  }

  // Trays through the machine, as in the general assembly: empty trays queue on
  // the sloping infeed rollers; one runs in on the tray belt to the end stop
  // beside the arm and is filled there; the ejector arm pushes the full tray
  // across onto the powered rollers, which send it back out onto the outfeed
  // rollers, where it runs down to the end stop and is lifted off.
  function moveTrays(sim, dt) {
    const st = CELL.station, slopeIn = Math.atan2(CELL.trayIn.topFar - CELL.trayIn.topNear, CELL.trayIn.x1 - CELL.trayIn.x0);
    const settle = (unit, x, outside, insideTop, slope) => {
      unit.position.x = x;
      unit.position.y = laneTop(outside, insideTop, x);
      unit.rotation.z = x > outside.x0 + 200 ? slope : 0;
    };
    const toward = (from, to, step) => (Math.abs(to - from) <= step ? to : from + Math.sign(to - from) * step);

    if (sim.crate.changing) {
      const k = Math.min(1, sim.crate.changeElapsed / sim.config.crateChangeS);
      const from = state.station.userData.startX != null ? state.station.userData.startX : WAIT_X;
      settle(state.station, from + (st.x - from) * smooth(Math.max(0, (k - 0.25) / 0.75)), CELL.trayIn, CELL.trayBelt.top, slopeIn);
    } else {
      settle(state.station, st.x, CELL.trayIn, CELL.trayBelt.top, slopeIn);
    }
    state.waiting.forEach((unit, i) => settle(unit, toward(unit.position.x, WAIT_X + i * UNIT_PITCH, 700 * dt), CELL.trayIn, CELL.trayBelt.top, slopeIn));

    const slopeOut = Math.atan2(CELL.trayOut.topFar - CELL.trayOut.topNear, CELL.trayOut.x1 - CELL.trayOut.x0);
    const pushS = Math.min(1.2, sim.config.crateChangeS * 0.3);
    state.leaving.forEach((tray, i) => {
      const age = sim.time - tray.since, unit = tray.unit;
      if (age < pushS) {        // ejector arm: across to the powered rollers
        unit.position.z = st.z + (CELL.rollers.z - st.z) * smooth(age / pushS);
        unit.position.y = st.floor + (CELL.rollers.top - st.floor) * (age / pushS);
        return;
      }
      unit.position.z = CELL.rollers.z;
      const park = CELL.trayOut.x1 - 60 - 230 - i * UNIT_PITCH;
      settle(unit, toward(unit.position.x, park, 650 * dt), CELL.trayOut, CELL.rollers.top, slopeOut);
      if (unit.position.x === park && tray.parkedAt == null) tray.parkedAt = sim.time;
    });
    // The tray against the end stop is lifted off after a while; the rest run down.
    const first = state.leaving[0];
    if (first && first.parkedAt != null && (sim.time - first.parkedAt > 8 || state.leaving.length > 4)) {
      trayLayer.remove(first.unit);
      state.leaving.shift();
      state.leaving.forEach((tray) => { tray.parkedAt = null; });
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
    moveTrays(sim, dt);
    const span = CELL.belt.x1 - CELL.belt.x0;
    cleats.forEach((cleat, i) => {
      cleat.position.x = CELL.belt.x0 + ((sim.time * sim.config.beltSpeed + (i * span) / cleats.length) % span);
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
    updatePlan();
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
  for (const button of document.querySelectorAll(".speeds button")) {
    button.addEventListener("click", () => {
      state.speed = +button.dataset.speed;
      for (const other of document.querySelectorAll(".speeds button")) other.setAttribute("aria-pressed", String(other === button));
    });
  }
  function setRunning(on) {
    state.running = on;
    $("pause").setAttribute("aria-pressed", String(!on));   // the pressed state shows the play glyph
    $("pause").setAttribute("aria-label", on ? "Pause" : "Play");
    $("pause").title = on ? "Pause (space bar)" : "Play (space bar)";
    document.body.classList.toggle("paused", !on);
  }
  $("pause").addEventListener("click", () => setRunning(!state.running));
  // Space bar pauses and plays, unless a slider or list that uses the space bar has the focus.
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    e.preventDefault();
    setRunning(!state.running);
  });
  $("reset").addEventListener("click", restart);
  $("axes").addEventListener("change", () => { axes.visible = $("axes").checked; });
  function showValues() {
    $("infeedV").textContent = $("infeed").value + " / min";
    $("beltV").textContent = $("belt").value + " mm/s";
    $("cycleV").textContent = (+$("cycle").value).toFixed(2) + " s";
    $("changeV").textContent = (+$("change").value).toFixed(1) + " s";
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
  window.pikpak = Object.assign(state, { setRunning, head, orbit, placeCamera, CELL, advance, showStats });   // for poking at from the console
  requestAnimationFrame(frame);
})();
