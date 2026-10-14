/*
 * Tests for the physics engine: does it obey the laws of physics?      Run:  node test_physics.js
 */
"use strict";
const P = require("./physics.js");
const { World, Body, DistanceJoint, V, box, circle } = P;

let passed = 0, failed = 0;
function check(label, ok, detail = "") {
  if (ok) { passed++; console.log("  PASS  " + label); }
  else { failed++; console.log("  FAIL  " + label + "  " + detail); }
}
const close = (a, b, rel) => Math.abs(a - b) <= rel * Math.max(Math.abs(b), 1e-9);
const G = 9.8, DT = 1 / 60;
function ground(w, opts = {}) {
  return w.add(new Body(box(200, 2), 0, -1, { static: true, friction: opts.friction ?? 0.6, restitution: opts.restitution ?? 0 }));
}
function run(w, seconds) { for (let i = 0; i < Math.round(seconds / DT); i++) w.step(DT); }

// ============================================================================ motion
function motion() {
  console.log("Motion and collisions:");
  let w = new World({ damping: 0 });
  const ball = w.add(new Body(circle(0.5), 0, 100));
  run(w, 2);
  const expected = 100 - 0.5 * G * 4;
  check(`free fall: after 2 s the ball is at ${ball.pos.y.toFixed(2)} m (formula: ${expected.toFixed(2)} m)`, close(ball.pos.y, expected, 0.005));

  w = new World({ gravity: V(0, 0), damping: 0 });
  const a = w.add(new Body(circle(0.5), -3, 0, { restitution: 1, friction: 0 }));
  const b = w.add(new Body(circle(0.5), 3, 0, { restitution: 1, friction: 0 }));
  a.vel = V(4, 0);
  const p0 = w.totalMomentum().x, e0 = w.kineticEnergy();
  run(w, 3);
  check(`elastic head-on collision of equal masses: velocities swap (${a.vel.x.toFixed(3)}, ${b.vel.x.toFixed(3)})`,
    Math.abs(a.vel.x) < 0.05 && close(b.vel.x, 4, 0.01));
  check(`momentum conserved (${p0.toFixed(4)} -> ${w.totalMomentum().x.toFixed(4)}) and energy kept within 1% (${e0.toFixed(3)} -> ${w.kineticEnergy().toFixed(3)} J)`,
    close(w.totalMomentum().x, p0, 1e-9) && close(w.kineticEnergy(), e0, 0.01));

  w = new World({ gravity: V(0, 0), damping: 0 });
  const c = w.add(new Body(circle(0.5), -3, 0.3, { restitution: 0, friction: 0, density: 3 }));
  const d = w.add(new Body(circle(0.5), 3, 0, { restitution: 0, friction: 0 }));
  c.vel = V(5, 1);
  d.vel = V(-2, 0);
  const before = w.totalMomentum();
  run(w, 3);
  const after = w.totalMomentum();
  check("off-centre collision of unequal masses: momentum conserved in x and y",
    close(after.x, before.x, 1e-9) && Math.abs(after.y - before.y) < 1e-9);

  const drops = [];
  for (const e of [0, 0.5, 0.9]) {
    w = new World({ damping: 0 });
    ground(w, { restitution: e });
    const bb = w.add(new Body(circle(0.5), 0, 5.5, { restitution: e }));
    let peak = 0, bounced = false;
    for (let i = 0; i < 240; i++) { w.step(DT); if (bb.vel.y > 0) bounced = true; if (bounced) peak = Math.max(peak, bb.pos.y - 0.5); }
    drops.push(peak);
  }
  check(`bounciness: dropped from 5 m, balls with restitution 0 / 0.5 / 0.9 rebound to ${drops.map((x) => x.toFixed(2)).join(" / ")} m (expected 0 / 1.25 / 4.05)`,
    drops[0] < 0.05 && close(drops[1], 1.25, 0.1) && close(drops[2], 4.05, 0.08));
}

// ============================================================================ friction
function friction() {
  console.log("\nFriction:");
  let w = new World({ damping: 0 });
  ground(w, { friction: 0.5 });
  const b = w.add(new Body(box(1, 1), 0, 0.5, { friction: 0.5 }));
  b.vel = V(6, 0);
  run(w, 3);
  const expected = 36 / (2 * 0.5 * G);
  check(`a box sliding at 6 m/s stops after ${b.pos.x.toFixed(2)} m (formula v²/2μg: ${expected.toFixed(2)} m)`, close(b.pos.x, expected, 0.04));

  for (const [deg, mu, shouldSlide] of [[20, 0.5, false], [35, 0.5, true]]) {
    w = new World({ damping: 0 });
    const th = deg * Math.PI / 180;
    w.add(new Body(box(60, 1), 0, 0, { static: true, angle: th, friction: mu }));
    const n = V(-Math.sin(th), Math.cos(th));
    const blk = w.add(new Body(box(1, 1), n.x * 1.0, n.y * 1.0, { angle: th, friction: mu }));
    run(w, 0.5);
    const start = { ...blk.pos };
    const v0 = Math.hypot(blk.vel.x, blk.vel.y);
    run(w, 1);
    const moved = Math.hypot(blk.pos.x - start.x, blk.pos.y - start.y);
    const accel = (Math.hypot(blk.vel.x, blk.vel.y) - v0) / 1;
    const theory = G * (Math.sin(th) - mu * Math.cos(th));
    if (!shouldSlide) check(`${deg}° slope, μ=${mu} (tan θ = ${Math.tan(th).toFixed(2)} < μ): the box stays put (moved ${(moved * 1000).toFixed(1)} mm)`, moved < 0.01);
    else check(`${deg}° slope, μ=${mu} (tan θ = ${Math.tan(th).toFixed(2)} > μ): it slides with a = ${accel.toFixed(2)} m/s² (formula g(sin θ − μ cos θ) = ${theory.toFixed(2)})`, close(accel, theory, 0.05));
  }
}

// ============================================================================ joints
function joints() {
  console.log("\nJoints:");
  const L = 4;
  const w = new World({ damping: 0 });
  const anchor = w.add(new Body(circle(0.1), 0, 10, { static: true }));
  const bob = w.add(new Body(circle(0.2), Math.sin(0.1) * L, 10 - Math.cos(0.1) * L, { density: 50 }));
  w.addJoint(new DistanceJoint(anchor, bob, anchor.pos, bob.pos));
  const crossings = [];
  let prevX = bob.pos.x;
  for (let i = 0; i < 60 * 30; i++) {
    w.step(DT);
    if (prevX > 0 && bob.pos.x <= 0) crossings.push(w.time);
    prevX = bob.pos.x;
  }
  const period = (crossings[crossings.length - 1] - crossings[0]) / (crossings.length - 1);
  const theory = 2 * Math.PI * Math.sqrt(L / G);
  check(`pendulum (4 m rod, small swing) period ${period.toFixed(3)} s vs 2π√(L/g) = ${theory.toFixed(3)} s`, close(period, theory, 0.02));
  const length = Math.hypot(bob.pos.x - anchor.pos.x, bob.pos.y - anchor.pos.y);
  check(`the rod keeps its length after 30 s: ${length.toFixed(4)} m`, close(length, L, 0.002));

  const w2 = new World({ damping: 0 });
  const top = w2.add(new Body(circle(0.1), 0, 10, { static: true }));
  const weight = w2.add(new Body(box(0.5, 0.5), 1, 9));
  w2.addJoint(new DistanceJoint(top, weight, top.pos, weight.pos, { rope: true, length: 3 }));
  let maxLen = 0, minLen = Infinity;
  for (let i = 0; i < 600; i++) { w2.step(DT); const d = Math.hypot(weight.pos.x - top.pos.x, weight.pos.y - top.pos.y); maxLen = Math.max(maxLen, d); minLen = Math.min(minLen, d); }
  check(`a 3 m rope goes slack when short (min ${minLen.toFixed(2)} m) but never stretches past 3 m (max ${maxLen.toFixed(3)} m)`, minLen < 2 && maxLen < 3.03);
}

// ============================================================================ stacking
function stacking() {
  console.log("\nStacking:");
  const setups = {
    "column of 10 boxes": (w) => [...Array(10).keys()].map((i) => w.add(new Body(box(1, 1), 0, 0.5 + i))),
    "pyramid of 120 boxes (15 rows)": (w) => {
      const out = [];
      for (let r = 0; r < 15; r++) for (let i = 0; i < 15 - r; i++) out.push(w.add(new Body(box(1, 1), (i - (15 - r - 1) / 2) * 1.05, 0.5 + r, { friction: 0.6 })));
      return out;
    },
    "wall of 60 bricks (running bond)": (w) => {
      const out = [];
      for (let r = 0; r < 10; r++) for (let i = 0; i < 6 - (r % 2); i++) out.push(w.add(new Body(box(2, 0.5), (i - 2.5) * 2 + (r % 2) * 1, 0.25 + r * 0.5, { friction: 0.7 })));
      return out;
    },
  };
  for (const [label, setup] of Object.entries(setups)) {
    const w = new World();
    ground(w);
    const bodies = setup(w);
    const start = bodies.map((b) => ({ ...b.pos }));
    const t0 = Date.now();
    run(w, 10);
    const ms = (Date.now() - t0) / 600;
    const drift = Math.max(...bodies.map((b, i) => Math.hypot(b.pos.x - start[i].x, b.pos.y - start[i].y)));
    const speed = Math.max(...bodies.map((b) => Math.hypot(b.vel.x, b.vel.y)));
    check(`${label} stands for 10 s: max drift ${(drift * 1000).toFixed(1)} mm, comes to rest (${ms.toFixed(1)} ms per frame)`, drift < 0.02 && speed < 0.01);
  }
}

// ============================================================================ collision detection
function satFuzz(n = 3000) {
  console.log("\nCollision detection (SAT) vs a brute-force geometry check:");
  let seed = 11;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  function randomConvex() {
    const k = 3 + Math.floor(rnd() * 5), r = 0.4 + rnd() * 1.2;
    const angles = [...Array(k)].map(() => rnd() * Math.PI * 2).sort((x, y) => x - y);
    return { type: "poly", verts: angles.map((a) => V(Math.cos(a) * r, Math.sin(a) * r)) };
  }
  const segDist = (p, a, b) => {
    const ab = P.sub(b, a), t = Math.max(0, Math.min(1, P.dot(P.sub(p, a), ab) / P.dot(ab, ab)));
    return P.len(P.sub(p, P.add(a, P.scale(ab, t))));
  };
  const segsCross = (a, b, c, d) => {
    const o = (p, q, r) => Math.sign(P.cross(P.sub(q, p), P.sub(r, p)));
    return o(a, b, c) * o(a, b, d) < 0 && o(c, d, a) * o(c, d, b) < 0;
  };
  let wrong = 0, overlaps = 0, example = "";
  for (let i = 0; i < n; i++) {
    const A = new Body(randomConvex(), 0, 0, { angle: rnd() * 6 });
    const B = new Body(randomConvex(), (rnd() - 0.5) * 4, (rnd() - 0.5) * 4, { angle: rnd() * 6 });
    if (P.polygonMassProps(A.shape.verts).area < 0.05 || P.polygonMassProps(B.shape.verts).area < 0.05) continue;
    const edges = (X) => X.world.map((v, j) => [v, X.world[(j + 1) % X.world.length]]);
    const overlap = A.world.some((p) => B.containsPoint(p)) || B.world.some((p) => A.containsPoint(p)) ||
      edges(A).some(([a, b]) => edges(B).some(([c, d]) => segsCross(a, b, c, d)));
    const dist = overlap ? 0 : Math.min(...A.world.flatMap((p) => edges(B).map(([c, d]) => segDist(p, c, d))),
      ...B.world.flatMap((p) => edges(A).map(([c, d]) => segDist(p, c, d))));
    const m = P.collide(A, B);
    overlaps += overlap;
    const bad = (overlap && (!m || Math.max(...m.contacts.map((c) => c.depth)) <= 0)) || (!overlap && dist > P.MARGIN + 1e-9 && m);
    if (bad) { wrong++; if (!example) example = `overlap=${overlap} distance=${dist.toFixed(4)} manifold=${JSON.stringify(m && m.contacts.map((c) => c.depth))}`; }
  }
  check(`${n} random convex polygon pairs (${overlaps} overlapping): contacts found exactly when shapes overlap or are within the 2 cm margin`, wrong === 0, `${wrong} wrong, e.g. ${example}`);
}

function determinism() {
  console.log("\nDeterminism:");
  const build = () => {
    const w = new World();
    ground(w);
    for (let i = 0; i < 40; i++) w.add(new Body(i % 2 ? circle(0.4) : box(0.8, 0.8), (i % 8) - 4, 2 + Math.floor(i / 8) * 1.2, { angle: i * 0.3 }));
    run(w, 5);
    return w.bodies.map((b) => [b.pos.x, b.pos.y, b.angle].join(",")).join(";");
  };
  check("the same scene simulated twice gives bit-for-bit identical results", build() === build());
}

motion();
friction();
joints();
stacking();
satFuzz();
determinism();
console.log(`\n${passed} passed, ${failed} failed`);
console.log(failed === 0 ? "ALL TESTS PASSED" : "SOME TESTS FAILED");
process.exit(failed ? 1 : 0);
