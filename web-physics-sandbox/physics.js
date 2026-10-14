/*
 * A 2D rigid-body physics engine (Box2D-style), from scratch. Units: meters, kilograms, seconds.
 *
 *   each step:  gravity -> broad phase (sort & sweep) -> narrow phase (SAT + clipping) -> contact manifolds
 *               -> warm start -> sequential impulses (contacts + joints) -> integrate positions
 *
 * Works in the browser (window.Physics) and in Node (require).
 */
(function (root) {
  "use strict";

  // ------------------------------------------------------------------ 2D vector helpers (plain objects, no allocation-heavy classes)
  const V = (x, y) => ({ x, y });
  const add = (a, b) => V(a.x + b.x, a.y + b.y);
  const sub = (a, b) => V(a.x - b.x, a.y - b.y);
  const scale = (a, s) => V(a.x * s, a.y * s);
  const dot = (a, b) => a.x * b.x + a.y * b.y;
  const cross = (a, b) => a.x * b.y - a.y * b.x;                 // 2D cross product (a scalar)
  const crossSV = (s, v) => V(-s * v.y, s * v.x);                // angular velocity × radius
  const len = (a) => Math.hypot(a.x, a.y);
  const rotate = (v, c, s) => V(c * v.x - s * v.y, s * v.x + c * v.y);

  // ------------------------------------------------------------------ bodies
  let nextId = 1;

  class Body {
    constructor(shape, x, y, opts = {}) {
      this.id = nextId++;
      this.shape = shape;                    // { type: "circle", r } or { type: "poly", verts: [...] } (counter-clockwise)
      this.pos = V(x, y);
      this.angle = opts.angle || 0;
      this.vel = V(0, 0);
      this.angVel = 0;
      this.force = V(0, 0);
      this.torque = 0;
      this.friction = opts.friction ?? 0.5;
      this.restitution = opts.restitution ?? 0.1;
      this.color = opts.color || null;
      this.static = !!opts.static;
      const density = opts.density ?? 1;
      if (shape.type === "circle") {
        this.mass = Math.PI * shape.r * shape.r * density;
        this.inertia = 0.5 * this.mass * shape.r * shape.r;
      } else {
        const { area, inertia } = polygonMassProps(shape.verts);
        this.mass = area * density;
        this.inertia = inertia * density;
      }
      this.invMass = this.static ? 0 : 1 / this.mass;
      this.invI = this.static ? 0 : 1 / this.inertia;
      this.updateTransform();
    }

    updateTransform() {
      const c = Math.cos(this.angle), s = Math.sin(this.angle);
      this.cos = c; this.sin = s;
      if (this.shape.type === "poly") {
        this.world = this.shape.verts.map((v) => add(this.pos, rotate(v, c, s)));
        this.normals = this.world.map((v, i) => {
          const w = this.world[(i + 1) % this.world.length];
          const e = sub(w, v), l = len(e);
          return V(e.y / l, -e.x / l);                          // outward normal for a counter-clockwise polygon
        });
        let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (const p of this.world) { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); }
        this.aabb = { minX, minY, maxX, maxY };
      } else {
        const r = this.shape.r;
        this.aabb = { minX: this.pos.x - r, minY: this.pos.y - r, maxX: this.pos.x + r, maxY: this.pos.y + r };
      }
    }

    worldPoint(local) { return add(this.pos, rotate(local, this.cos, this.sin)); }
    localPoint(world) { const d = sub(world, this.pos); return rotate(d, this.cos, -this.sin); }

    kineticEnergy() { return 0.5 * this.mass * dot(this.vel, this.vel) + 0.5 * this.inertia * this.angVel * this.angVel; }

    containsPoint(p) {
      if (this.shape.type === "circle") return len(sub(p, this.pos)) <= this.shape.r;
      return this.normals.every((n, i) => dot(n, sub(p, this.world[i])) <= 0);
    }
  }

  /** Area and moment of inertia (about the centroid, density 1) of a polygon, by summing triangles. */
  function polygonMassProps(verts) {
    let area = 0, I = 0;
    for (let i = 0; i < verts.length; i++) {
      const a = verts[i], b = verts[(i + 1) % verts.length];
      const c = cross(a, b);
      area += c / 2;
      I += c * (dot(a, a) + dot(a, b) + dot(b, b)) / 12;
    }
    return { area: Math.abs(area), inertia: Math.abs(I) };
  }

  const box = (w, h) => ({ type: "poly", verts: [V(-w / 2, -h / 2), V(w / 2, -h / 2), V(w / 2, h / 2), V(-w / 2, h / 2)] });
  const circle = (r) => ({ type: "circle", r });
  const regular = (n, r) => ({ type: "poly", verts: Array.from({ length: n }, (_, i) => V(r * Math.cos(2 * Math.PI * i / n), r * Math.sin(2 * Math.PI * i / n))) });

  // ------------------------------------------------------------------ narrow phase: contact generation
  // Every function returns { normal (from A to B), contacts: [{ point, depth, id }] } or null.
  // Speculative contacts: shapes up to MARGIN apart already produce contacts (with negative depth). The solver
  // lets them close that gap but not pass it, so resting contacts don't flicker on and off as bodies jiggle.
  const MARGIN = 0.02;

  function collideCircles(a, b) {
    const d = sub(b.pos, a.pos), dist = len(d), r = a.shape.r + b.shape.r;
    if (dist >= r + MARGIN) return null;
    const n = dist > 1e-9 ? scale(d, 1 / dist) : V(0, 1);
    const pa = add(a.pos, scale(n, a.shape.r)), pb = sub(b.pos, scale(n, b.shape.r));
    return { normal: n, contacts: [{ point: scale(add(pa, pb), 0.5), depth: r - dist, id: 0, pa, pb }] };
  }

  function collidePolyCircle(poly, circ) {
    const c = circ.pos, r = circ.shape.r;
    let best = -Infinity, bi = 0;
    for (let i = 0; i < poly.world.length; i++) {
      const s = dot(poly.normals[i], sub(c, poly.world[i]));
      if (s > r + MARGIN) return null;                          // a separating axis: no contact
      if (s > best) { best = s; bi = i; }
    }
    const v1 = poly.world[bi], v2 = poly.world[(bi + 1) % poly.world.length];
    if (best < 1e-9) {                                           // circle center is inside the polygon
      const n = poly.normals[bi];
      const pb = sub(c, scale(n, r)), pa = sub(c, scale(n, best));
      return { normal: n, contacts: [{ point: pb, depth: r - best, id: bi, pa, pb }] };
    }
    const u1 = dot(sub(c, v1), sub(v2, v1)), u2 = dot(sub(c, v2), sub(v1, v2));
    let closest;
    if (u1 <= 0) closest = v1;                                   // vertex region
    else if (u2 <= 0) closest = v2;
    else {
      const n = poly.normals[bi], pb = sub(c, scale(n, r)), pa = sub(c, scale(n, best));
      return { normal: n, contacts: [{ point: pb, depth: r - best, id: bi, pa, pb }] };
    }
    const d = sub(c, closest), dist = len(d);
    if (dist > r + MARGIN) return null;
    const n = dist > 1e-9 ? scale(d, 1 / dist) : poly.normals[bi];
    return { normal: n, contacts: [{ point: closest, depth: r - dist, id: 100 + bi, pa: closest, pb: sub(c, scale(n, r)) }] };
  }

  /** Separating axis test: the edge of A whose outward normal separates the polygons the most. */
  function maxSeparation(A, B) {
    let best = -Infinity, bi = 0;
    for (let i = 0; i < A.world.length; i++) {
      const n = A.normals[i], v = A.world[i];
      let min = Infinity;
      for (const p of B.world) min = Math.min(min, dot(n, sub(p, v)));
      if (min > best) { best = min; bi = i; }
    }
    return { sep: best, edge: bi };
  }

  function clipSegment(pts, n, offset, clipEdge) {
    const out = [];
    const d0 = dot(n, pts[0].p) - offset, d1 = dot(n, pts[1].p) - offset;
    if (d0 <= 0) out.push(pts[0]);
    if (d1 <= 0) out.push(pts[1]);
    if (d0 * d1 < 0) {
      const t = d0 / (d0 - d1);
      out.push({ p: add(pts[0].p, scale(sub(pts[1].p, pts[0].p), t)), id: pts[d0 > 0 ? 0 : 1].id + clipEdge * 1000 });
    }
    return out;
  }

  /** Polygon vs polygon: SAT, then clip the incident edge against the reference face (like Box2D). */
  function collidePolys(A, B) {
    const sa = maxSeparation(A, B);
    if (sa.sep > MARGIN) return null;
    const sb = maxSeparation(B, A);
    if (sb.sep > MARGIN) return null;
    let ref, inc, edge, flip;
    if (sb.sep > sa.sep * 0.95 + 0.001 * 0.5) { ref = B; inc = A; edge = sb.edge; flip = true; }
    else { ref = A; inc = B; edge = sa.edge; flip = false; }
    const n = ref.normals[edge];
    let ie = 0, min = Infinity;                                  // incident edge: the one facing most against n
    for (let i = 0; i < inc.normals.length; i++) {
      const d = dot(n, inc.normals[i]);
      if (d < min) { min = d; ie = i; }
    }
    let pts = [{ p: inc.world[ie], id: ie }, { p: inc.world[(ie + 1) % inc.world.length], id: (ie + 1) % inc.world.length }];
    const v1 = ref.world[edge], v2 = ref.world[(edge + 1) % ref.world.length];
    const tangent = scale(sub(v2, v1), 1 / len(sub(v2, v1)));
    pts = clipSegment(pts, scale(tangent, -1), -dot(tangent, v1), 1);
    if (pts.length >= 2) pts = clipSegment(pts, tangent, dot(tangent, v2), 2);
    if (pts.length < 2) {
      // The incident edge was clipped away entirely (thin shapes meeting near a corner). Fall back to the
      // single deepest vertex of the incident polygon so overlapping shapes always produce a contact.
      let deepest = null, best = Infinity;
      inc.world.forEach((p, i) => { const sep = dot(n, sub(p, v1)); if (sep < best) { best = sep; deepest = { p, id: i }; } });
      if (best > MARGIN) return null;
      const onRef = sub(deepest.p, scale(n, best));
      return { normal: flip ? scale(n, -1) : n, contacts: [{ point: onRef, depth: -best, id: 90000 + deepest.id,
               pa: flip ? deepest.p : onRef, pb: flip ? onRef : deepest.p }] };
    }
    const contacts = [];
    for (const c of pts) {
      const sep = dot(n, sub(c.p, v1));
      // Put the contact ON the reference face (like Box2D). Both points of a manifold then lie on one line, so
      // equal-and-opposite friction impulses at the two corners cancel instead of twisting a tilted box.
      if (sep <= MARGIN) {
        const onRef = sub(c.p, scale(n, sep));
        contacts.push({ point: onRef, depth: -sep, id: edge * 100 + c.id + (flip ? 50000 : 0),
                        pa: flip ? c.p : onRef, pb: flip ? onRef : c.p });
      }
    }
    if (!contacts.length) return null;
    return { normal: flip ? scale(n, -1) : n, contacts };
  }

  function collide(a, b) {
    const ta = a.shape.type, tb = b.shape.type;
    if (ta === "circle" && tb === "circle") return collideCircles(a, b);
    if (ta === "poly" && tb === "poly") return collidePolys(a, b);
    if (ta === "poly") return collidePolyCircle(a, b);
    const m = collidePolyCircle(b, a);
    if (m) {
      m.normal = scale(m.normal, -1);
      for (const c of m.contacts) [c.pa, c.pb] = [c.pb, c.pa];       // anchors belong to (circle, poly) now
    }
    return m;
  }

  // ------------------------------------------------------------------ joints
  /** Keeps two anchor points a fixed distance apart (a rod), or at most that far (a rope). */
  class DistanceJoint {
    constructor(a, b, worldA, worldB, opts = {}) {
      this.a = a; this.b = b;
      this.localA = a.localPoint(worldA);
      this.localB = b.localPoint(worldB);
      this.length = opts.length ?? len(sub(worldB, worldA));
      this.rope = !!opts.rope;
      this.impulse = 0;
    }
    preStep(invDt) {
      const { a, b } = this;
      this.rA = rotate(this.localA, a.cos, a.sin);
      this.rB = rotate(this.localB, b.cos, b.sin);
      const d = sub(add(b.pos, this.rB), add(a.pos, this.rA));
      const l = len(d);
      this.n = l > 1e-9 ? scale(d, 1 / l) : V(1, 0);
      this.C = l - this.length;
      this.active = !this.rope || this.C > 0;
      if (!this.active) { this.impulse = 0; return; }
      const rnA = cross(this.rA, this.n), rnB = cross(this.rB, this.n);
      const k = a.invMass + b.invMass + a.invI * rnA * rnA + b.invI * rnB * rnB;
      this.mass = k > 0 ? 1 / k : 0;
      this.bias = -0.2 * invDt * this.C;
      applyImpulse(a, b, this.rA, this.rB, scale(this.n, this.impulse));            // warm start
    }
    solve() {
      if (!this.active) return;
      const { a, b } = this;
      const dv = sub(add(b.vel, crossSV(b.angVel, this.rB)), add(a.vel, crossSV(a.angVel, this.rA)));
      let dP = this.mass * (-dot(dv, this.n) + this.bias);
      if (this.rope) { const old = this.impulse; this.impulse = Math.min(old + dP, 0); dP = this.impulse - old; }
      else this.impulse += dP;
      applyImpulse(a, b, this.rA, this.rB, scale(this.n, dP));
    }
  }

  function applyImpulse(a, b, rA, rB, P) {
    a.vel = sub(a.vel, scale(P, a.invMass)); a.angVel -= a.invI * cross(rA, P);
    b.vel = add(b.vel, scale(P, b.invMass)); b.angVel += b.invI * cross(rB, P);
  }

  // ------------------------------------------------------------------ the world
  class World {
    constructor(opts = {}) {
      this.damping = opts.damping ?? 0.02;
      this.bodies = [];
      this.joints = [];
      this.gravity = opts.gravity ?? V(0, -9.8);
      this.iterations = opts.iterations ?? 5;
      this.substeps = opts.substeps ?? 6;      // several small steps per frame: the key to stable stacks
      this.arbiters = new Map();          // "idA:idB" -> contact manifold that persists between steps
      this.mouse = null;                  // { body, local, target }
      this.time = 0;
    }

    add(body) { this.bodies.push(body); return body; }
    addJoint(j) { this.joints.push(j); return j; }
    remove(body) {
      this.bodies = this.bodies.filter((b) => b !== body);
      this.joints = this.joints.filter((j) => j.a !== body && j.b !== body);
    }

    bodyAt(p) { for (let i = this.bodies.length - 1; i >= 0; i--) if (!this.bodies[i].static && this.bodies[i].containsPoint(p)) return this.bodies[i]; return null; }

    /** Broad phase: sort by left edge and sweep, so only boxes that overlap on x are compared. */
    candidatePairs() {
      const list = [...this.bodies].sort((a, b) => a.aabb.minX - b.aabb.minX);
      const pairs = [];
      for (let i = 0; i < list.length; i++) {
        const a = list[i];
        for (let j = i + 1; j < list.length; j++) {
          const b = list[j];
          if (b.aabb.minX > a.aabb.maxX + MARGIN) break;
          if (a.static && b.static) continue;
          if (b.aabb.minY > a.aabb.maxY + MARGIN || a.aabb.minY > b.aabb.maxY + MARGIN) continue;
          pairs.push(a.id < b.id ? [a, b] : [b, a]);
        }
      }
      return pairs;
    }

    /** Advance the world by dt seconds, split into `substeps` small steps. */
    step(dt) {
      for (let i = 0; i < this.substeps; i++) this.substep(dt / this.substeps);
    }

    substep(dt) {
      const invDt = dt > 0 ? 1 / dt : 0;
      // 1. forces -> velocities
      for (const b of this.bodies) {
        if (b.static) continue;
        b.vel = add(b.vel, scale(add(this.gravity, scale(b.force, b.invMass)), dt));
        b.angVel += dt * b.invI * b.torque;
        b.vel = scale(b.vel, 1 / (1 + dt * this.damping));     // a little air resistance
        b.angVel *= 1 / (1 + dt * this.damping * 2.5);
        b.force = V(0, 0); b.torque = 0;
      }
      if (this.mouse && this.bodies.includes(this.mouse.body)) {   // drag: a stiff spring-damper toward the cursor
        const { body, local, target } = this.mouse;
        const p = body.worldPoint(local), r = sub(p, body.pos);
        const v = add(body.vel, crossSV(body.angVel, r));
        const omega = 2 * Math.PI * 5, zeta = 0.7, m = body.mass;
        const F = sub(scale(sub(target, p), m * omega * omega), scale(v, 2 * m * zeta * omega));
        const P = scale(F, dt);
        body.vel = add(body.vel, scale(P, body.invMass)); body.angVel += body.invI * cross(r, P);
      }
      // 2. collision detection; keep the accumulated impulses of contacts that persist (warm starting)
      const live = new Map();
      for (const [a, b] of this.candidatePairs()) {
        const m = collide(a, b);
        if (!m) continue;
        const key = a.id + ":" + b.id;
        const old = this.arbiters.get(key);
        const friction = Math.sqrt(a.friction * b.friction), restitution = Math.max(a.restitution, b.restitution);
        // Match each new contact to last step's contact at (almost) the same spot on body A, and inherit its
        // impulses. Matching by position is more robust than by feature id: when edges line up exactly, the
        // same physical point can come out of the clipper with a different id from one step to the next.
        const used = new Set();
        const contacts = m.contacts.map((c) => {
          const localA = a.localPoint(c.point);
          let prev = null, best = 0.05 * 0.05;
          for (const o of old ? old.contacts : []) {
            const dx = o.localA.x - localA.x, dy = o.localA.y - localA.y, d2 = dx * dx + dy * dy;
            if (d2 < best && !used.has(o)) { best = d2; prev = o; }
          }
          if (prev) used.add(prev);
          return { ...c, localA, la: a.localPoint(c.pa), lb: b.localPoint(c.pb), Pn: prev ? prev.Pn : 0, Pt: prev ? prev.Pt : 0 };
        });
        live.set(key, { a, b, normal: m.normal, contacts, friction, restitution });
      }
      this.arbiters = live;
      // 3. prepare contacts + joints
      for (const arb of live.values()) this.preStep(arb, invDt);
      for (const j of this.joints) j.preStep(invDt);
      // 4. sequential impulses
      for (let it = 0; it < this.iterations; it++) {
        for (const arb of live.values()) this.solveContact(arb);
        for (const j of this.joints) j.solve();
      }
      // 5. velocities -> positions
      for (const b of this.bodies) {
        if (b.static) continue;
        b.pos = add(b.pos, scale(b.vel, dt));
        b.angle += b.angVel * dt;
        b.cos = Math.cos(b.angle); b.sin = Math.sin(b.angle);
      }
      // 6. fix overlaps by nudging POSITIONS (Box2D's position solver). Doing this with extra velocity instead
      //    (Baumgarte) adds energy that warm starting carries into the next step, and tall stacks start to rock.
      for (let it = 0; it < 4; it++) for (const arb of live.values()) this.solvePosition(arb);
      for (const b of this.bodies) if (!b.static) b.updateTransform();
      this.time += dt;
    }

    preStep(arb, invDt) {
      const { a, b, normal: n } = arb;
      const t = V(n.y, -n.x);
      for (const c of arb.contacts) {
        c.rA = sub(c.point, a.pos); c.rB = sub(c.point, b.pos);
        const rnA = cross(c.rA, n), rnB = cross(c.rB, n), rtA = cross(c.rA, t), rtB = cross(c.rB, t);
        c.massN = 1 / (a.invMass + b.invMass + a.invI * rnA * rnA + b.invI * rnB * rnB);
        c.massT = 1 / (a.invMass + b.invMass + a.invI * rtA * rtA + b.invI * rtB * rtB);
        // speculative contacts may close their gap but no more; penetration is fixed later by moving positions
        c.bias = c.depth < 0 ? c.depth * invDt : 0;
        const dv = sub(add(b.vel, crossSV(b.angVel, c.rB)), add(a.vel, crossSV(a.angVel, c.rA)));
        const vn = dot(dv, n);
        // bounce: use the approach speed even if a speculative contact stopped the bodies just short of touching
        if (vn < -1) c.bias = Math.max(c.bias, -arb.restitution * vn);
        applyImpulse(a, b, c.rA, c.rB, add(scale(n, c.Pn), scale(t, c.Pt)));   // warm start
      }
    }

    solveContact(arb) {
      const { a, b, normal: n } = arb;
      const t = V(n.y, -n.x);
      const relVel = (c) => sub(add(b.vel, crossSV(b.angVel, c.rB)), add(a.vel, crossSV(a.angVel, c.rA)));
      // Friction first: non-penetration is more important, so the normal impulses get the last word.
      for (const c of arb.contacts) {
        let dPt = c.massT * -dot(relVel(c), t);
        const maxPt = arb.friction * c.Pn;                       // Coulomb friction cone
        const Pt0 = c.Pt;
        c.Pt = Math.max(-maxPt, Math.min(Pt0 + dPt, maxPt));
        dPt = c.Pt - Pt0;
        applyImpulse(a, b, c.rA, c.rB, scale(t, dPt));
      }
      if (arb.contacts.length === 2 && this.solveBlock(arb, relVel)) return;
      for (const c of arb.contacts) {                            // one point at a time
        let dPn = c.massN * (-dot(relVel(c), n) + c.bias);
        const Pn0 = c.Pn;
        c.Pn = Math.max(Pn0 + dPn, 0);                          // contacts can push, never pull
        dPn = c.Pn - Pn0;
        applyImpulse(a, b, c.rA, c.rB, scale(n, dPn));
      }
    }

    solvePosition(arb) {
      const { a, b, normal: n } = arb;
      // Work out every point's correction from the SAME starting state, then apply them together. Fixing the
      // left corner first and the right corner second would tilt the box a little on every pass.
      const pushes = [];
      for (const c of arb.contacts) {
        const pa = add(a.pos, rotate(c.la, a.cos, a.sin)), pb = add(b.pos, rotate(c.lb, b.cos, b.sin));
        const sep = dot(sub(pb, pa), n);                         // negative = overlapping
        const C = Math.max(-0.2, Math.min(0, 0.2 * (sep + 0.005)));   // fix 20% per pass, keep 5 mm of slop
        if (C >= 0) continue;
        const p = scale(add(pa, pb), 0.5), rA = sub(p, a.pos), rB = sub(p, b.pos);
        const rnA = cross(rA, n), rnB = cross(rB, n);
        const K = a.invMass + b.invMass + a.invI * rnA * rnA + b.invI * rnB * rnB;
        if (K > 0) pushes.push({ rA, rB, P: scale(n, -C / (K * arb.contacts.length)) });
      }
      for (const { rA, rB, P } of pushes) {
        a.pos = sub(a.pos, scale(P, a.invMass)); a.angle -= a.invI * cross(rA, P);
        b.pos = add(b.pos, scale(P, b.invMass)); b.angle += b.invI * cross(rB, P);
      }
      if (pushes.length) {
        a.cos = Math.cos(a.angle); a.sin = Math.sin(a.angle);
        b.cos = Math.cos(b.angle); b.sin = Math.sin(b.angle);
      }
    }

    /**
     * Solve both points of a 2-point manifold TOGETHER (Box2D's block solver). Sequential solving makes the two
     * corners of a resting box fight each other and tall stacks wobble; solving the 2x2 problem exactly does not.
     * We need impulses x >= 0 and resulting velocities vn >= bias, with x * (vn - bias) = 0 (a tiny LCP).
     */
    solveBlock(arb, relVel) {
      const { a, b, normal: n } = arb;
      const [c1, c2] = arb.contacts;
      const rn1A = cross(c1.rA, n), rn1B = cross(c1.rB, n), rn2A = cross(c2.rA, n), rn2B = cross(c2.rB, n);
      const m = a.invMass + b.invMass;
      const k11 = m + a.invI * rn1A * rn1A + b.invI * rn1B * rn1B;
      const k22 = m + a.invI * rn2A * rn2A + b.invI * rn2B * rn2B;
      const k12 = m + a.invI * rn1A * rn2A + b.invI * rn1B * rn2B;
      const det = k11 * k22 - k12 * k12;
      if (k11 * k11 >= 1000 * det) return false;                // nearly singular: points almost coincide
      const ax = c1.Pn, ay = c2.Pn;
      // b = vn - bias - K * accumulated
      const bx = dot(relVel(c1), n) - c1.bias - (k11 * ax + k12 * ay);
      const by = dot(relVel(c2), n) - c2.bias - (k12 * ax + k22 * ay);
      const apply = (x1, x2) => {
        const d1 = x1 - ax, d2 = x2 - ay;
        applyImpulse(a, b, c1.rA, c1.rB, scale(n, d1));
        applyImpulse(a, b, c2.rA, c2.rB, scale(n, d2));
        c1.Pn = x1; c2.Pn = x2;
        return true;
      };
      // Case 1: both points push.
      let x1 = (-k22 * bx + k12 * by) / det, x2 = (k12 * bx - k11 * by) / det;
      if (x1 >= 0 && x2 >= 0) return apply(x1, x2);
      // Case 2: only point 1 pushes; point 2 must then be separating.
      x1 = -bx / k11;
      if (x1 >= 0 && k12 * x1 + by >= 0) return apply(x1, 0);
      // Case 3: only point 2 pushes.
      x2 = -by / k22;
      if (x2 >= 0 && k12 * x2 + bx >= 0) return apply(0, x2);
      // Case 4: neither pushes.
      if (bx >= 0 && by >= 0) return apply(0, 0);
      return true;                                              // no exact solution this iteration: try again next
    }

    totalMomentum() {
      return this.bodies.reduce((p, b) => b.static ? p : add(p, scale(b.vel, b.mass)), V(0, 0));
    }
    kineticEnergy() { return this.bodies.reduce((e, b) => b.static ? e : e + b.kineticEnergy(), 0); }
  }

  const api = { MARGIN, World, Body, DistanceJoint, V, add, sub, scale, dot, cross, len, box, circle, regular, collide, polygonMassProps };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Physics = api;
})(typeof window !== "undefined" ? window : globalThis);
