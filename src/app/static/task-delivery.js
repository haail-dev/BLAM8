// Durable task announcements: hints never carry speech. Read, claim, buffer,
// read/authorize again, then one automatic playback attempt per event/session.
export function createTaskDelivery(deps) {
  let active = null;
  let busy = false;
  let quietSince = null;
  let generation = 0;
  const requests = new Set();
  const seen = new Set();
  const key = t => [t.id, t.revision, t.event_id, t.voice_session].join(':');
  const same = (a, b) => a && b && key(a) === key(b);
  async function bounded(request) {
    let timer;
    const controller = new AbortController();
    requests.add(controller);
    try {
      return await Promise.race([request(controller.signal), new Promise((_, reject) => {
        controller.signal.addEventListener('abort', () => reject(new Error('Task delivery aborted')), {once:true});
        timer = setTimeout(() => { controller.abort(); reject(new Error('Task delivery timeout')); }, 15000);
      })]);
    } finally { clearTimeout(timer); requests.delete(controller); controller.abort(); }
  }
  function interrupt() {
    generation += 1;
    quietSince = null;
    active?.abort.abort();
    for (const controller of requests) controller.abort();
  }
  async function tick() {
    if (active?.started) {
      if (deps.interrupted && deps.interrupted()) interrupt();
      return;
    }
    if (!deps.gap()) { interrupt(); return; }
    if (quietSince === null) quietSince = deps.now();
    if (busy || deps.now() - quietSince < 600) return;
    busy = true;
    const reservation = generation;
    let job;
    try {
      const tasks = await bounded(signal => deps.read(signal));
      if (!deps.gap() || reservation !== generation || quietSince === null || deps.now() - quietSince < 600) return;
      const task = tasks.find(t => t.speakable && t.event_id && !t.delivery?.state && !seen.has(key(t)));
      if (!task) return;
      job = { task, id: deps.id(), abort: new AbortController(), started: false, generation };
      active = job;
      const claimed = await bounded(signal => deps.transition(job, 'claim', undefined, signal));
      if (!claimed.ok || job.abort.signal.aborted) return;
      const audio = await bounded(() => deps.tts(claimed.task.outcome, job.abort.signal));
      // TTS can take seconds. New user audio/foreground work, permissions,
      // revision and durable delivery ownership all get checked again.
      const fresh = (await bounded(signal => deps.read(signal))).find(t => same(t, job.task));
      if (!audio || !fresh || !deps.gap() || job.abort.signal.aborted || job.generation !== generation) return;
      const started = await bounded(signal => deps.transition(job, 'start', undefined, signal));
      if (!started.ok || !deps.gap() || job.abort.signal.aborted || job.generation !== generation) return;
      job.started = true;
      seen.add(key(task));
      const completed = await deps.play(audio, job.abort.signal, started.task.outcome);
      await bounded(signal => deps.transition(job, 'ack', completed && !job.abort.signal.aborted ? 'completed' : 'interrupted', signal));
    } catch (_e) {
      // A lost transition response never licenses a second automatic attempt.
    } finally {
      if (job) {
        job.abort.abort();
        if (job.started) {
          seen.add(key(job.task));
          await bounded(signal => deps.transition(job, 'ack', 'interrupted', signal)).catch(() => {});
        } else {
          await bounded(signal => deps.transition(job, 'release', undefined, signal)).catch(() => {});
        }
      }
      if (active === job) active = null;
      busy = false;
    }
  }
  return { tick, interrupt };
}
