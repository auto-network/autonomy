// Implementation order for sibling beads. Only directed blocking edges order
// work; parent-child and relates-to describe relationships, not prerequisites.
(function () {
  window.beadImplementationOrder = function (beads) {
    const byId = new Map(beads.map(b => [b.id, b]));
    const pending = new Map(beads.map(b => [b.id, new Set()]));
    const dependents = new Map(beads.map(b => [b.id, new Set()]));
    for (const bead of beads) {
      for (const dep of bead.dependencies || []) {
        if ((dep.type || dep.dependency_type) !== 'blocks') continue;
        const prerequisite = dep.depends_on_id || dep.id;
        if (!byId.has(prerequisite)) continue;
        pending.get(bead.id).add(prerequisite);
        dependents.get(prerequisite).add(bead.id);
      }
    }
    const compare = (a, b) => (a.priority ?? 4) - (b.priority ?? 4)
      || a.id.localeCompare(b.id, undefined, {numeric: true});
    const ready = beads.filter(b => !pending.get(b.id).size).sort(compare);
    const ordered = [];
    const emitted = new Set();
    while (ready.length) {
      const bead = ready.shift();
      ordered.push(bead);
      emitted.add(bead.id);
      for (const id of dependents.get(bead.id)) {
        pending.get(id).delete(bead.id);
        if (!pending.get(id).size) ready.push(byId.get(id));
      }
      ready.sort(compare);
    }
    // Invalid cyclic data has no implementation order, but must not hide work.
    return ordered.concat(beads.filter(b => !emitted.has(b.id)).sort(compare));
  };
})();
