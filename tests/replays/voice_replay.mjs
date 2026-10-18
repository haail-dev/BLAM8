// Replays speech-service event sequences through the turn assembler with a
// fake clock (reports/Voice agent turn taking practices.md: "build a replay
// test before changing another threshold"). Run by tests/test_voice_replay.py;
// exits non-zero with the failing scenario's name.
import { createTurnAssembler, SALVAGE_MS, SETTLE_MS } from "../../src/app/static/voice-turns.js";

function rig() {
  const out = { utterances: [], events: [], display: "" };
  const a = createTurnAssembler({
    onUtterance: (u) => out.utterances.push(u),
    onDisplay: (t) => { out.display = t; },
    onEvent: (name, d) => out.events.push([name, d]),
  });
  let now = 1000;
  const at = (ms) => { now += ms; return now; };
  // Run the beat every 100 ms up to `ms` from now, as voice-live.js does.
  const run = (ms) => { const end = now + ms; while (now < end) { now += 100; a.tick(now); } };
  return { a, out, at, run, t: () => now };
}

const scenarios = {
  // Kris 2026-09-24: the fallback copied the streamed words at 4 s, then the
  // real transcript arrived and was added again.
  "late completed after salvage is not doubled"() {
    const { a, out, at, run, t } = rig();
    a.speechStarted({ itemId: "i1", now: at(0) });
    a.delta({ itemId: "i1", delta: "Do we have a draft waiting for", now: at(800) });
    a.speechStopped({ itemId: "i1", now: at(400) });
    a.committed({ itemId: "i1", now: at(50) });
    a.delta({ itemId: "i1", delta: " Laura Finch?", now: at(300) });
    run(SALVAGE_MS + SETTLE_MS + 200);
    a.completed({ itemId: "i1", transcript: "Do we have a draft waiting for Laura Finch?", now: t() });
    run(2000);
    eq(out.utterances.length, 1, "one utterance");
    eq(out.utterances[0].text, "Do we have a draft waiting for Laura Finch?", "text once");
    ok(out.events.some(([n]) => n === "late_ignored"), "the late transcript is noted, not used");
  },

  "a transcript after the salvage, while still talking, replaces it"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "i1", now: at(0) });
    a.delta({ itemId: "i1", delta: "Do we have a draft", now: at(500) });
    a.speechStopped({ itemId: "i1", now: at(300) });
    a.committed({ itemId: "i1", now: at(20) });
    a.speechStarted({ itemId: "i2", now: at(100) });      // still talking
    for (let i = 0; i < 4; i += 1) {                       // words keep streaming…
      a.delta({ itemId: "i2", delta: " for", now: at(100) });
      run(900);
    }                                                      // …past i1's salvage
    ok(out.events.some(([n]) => n === "salvaged"), "i1 was salvaged");
    a.completed({ itemId: "i1", transcript: "Do we have a draft waiting", now: at(50) });
    a.speechStopped({ itemId: "i2", now: at(100) });
    a.committed({ itemId: "i2", now: at(10) });
    a.completed({ itemId: "i2", transcript: "for Laura Finch?", now: at(200) });
    run(SETTLE_MS + 200);
    eq(out.utterances.length, 1, "one utterance");
    eq(out.utterances[0].text, "Do we have a draft waiting for Laura Finch?", "the final replaced the salvage");
  },

  "a pause mid-thought merges two segments into one turn"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "i1", now: at(0) });
    a.speechStopped({ itemId: "i1", now: at(700) });
    a.committed({ itemId: "i1", now: at(10) });
    a.speechStarted({ itemId: "i2", now: at(400) });     // talking again before i1 is transcribed
    a.completed({ itemId: "i1", transcript: "Good morning.", now: at(200) });
    a.speechStopped({ itemId: "i2", now: at(900) });
    a.committed({ itemId: "i2", now: at(10) });
    a.completed({ itemId: "i2", transcript: "What's on today?", now: at(400) });
    run(SETTLE_MS + 200);
    eq(out.utterances.length, 1, "one turn");
    eq(out.utterances[0].text, "Good morning. What's on today?", "in spoken order");
  },

  "transcripts arriving out of order still read in spoken order"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "i1", now: at(0) });
    a.speechStopped({ itemId: "i1", now: at(600) });
    a.committed({ itemId: "i1", now: at(10) });
    a.speechStarted({ itemId: "i2", now: at(300) });
    a.speechStopped({ itemId: "i2", now: at(600) });
    a.committed({ itemId: "i2", now: at(10) });
    a.completed({ itemId: "i2", transcript: "for tomorrow", now: at(100) });
    a.completed({ itemId: "i1", transcript: "Check the diary", now: at(100) });
    run(SETTLE_MS + 200);
    eq(out.utterances[0].text, "Check the diary for tomorrow", "i1 before i2");
  },

  "a one-word yes is a turn, loudness measured from before 'started'"() {
    const { a, out, at, run, t } = rig();
    // The word itself: loud, then quiet by the time the service says "started".
    for (let i = 0; i < 20; i += 1) a.notePeak(0.3, at(10));
    for (let i = 0; i < 20; i += 1) a.notePeak(0.01, at(10));
    a.speechStarted({ itemId: "y1", now: at(10) });
    a.speechStopped({ itemId: "y1", now: at(200) });
    a.committed({ itemId: "y1", now: at(10) });
    a.completed({ itemId: "y1", transcript: "Yes.", now: at(300) });
    run(SETTLE_MS + 200);
    eq(out.utterances.length, 1, "sent");
    eq(out.utterances[0].text, "Yes.", "the word");
    ok(out.utterances[0].peak >= 0.3, "its peak is the word's, not the silence after it");
    void t;
  },

  "struck speech: its late words never land in the next sentence"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "tv", now: at(0) });
    a.delta({ itemId: "tv", delta: "on BBC One tonight", now: at(300) });
    a.discard();
    a.completed({ itemId: "tv", transcript: "on BBC One tonight", now: at(100) });
    a.speechStarted({ itemId: "me", now: at(500) });
    a.speechStopped({ itemId: "me", now: at(800) });
    a.committed({ itemId: "me", now: at(10) });
    a.completed({ itemId: "me", transcript: "What's in the diary?", now: at(200) });
    run(SETTLE_MS + 200);
    eq(out.utterances.length, 1, "only mine");
    eq(out.utterances[0].text, "What's in the diary?", "no telly words");
  },

  "nothing transcribed still ends in an outcome"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "c1", now: at(0) });
    a.speechStopped({ itemId: "c1", now: at(200) });
    a.committed({ itemId: "c1", now: at(10) });
    a.completed({ itemId: "c1", transcript: "", now: at(300) });
    run(SETTLE_MS + 200);
    eq(out.utterances.length, 1, "an (empty) utterance is reported");
    eq(out.utterances[0].text, "", "empty — the screen says so, it doesn't sit on '…'");
  },

  "a segment the server never closes is let go"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "e1", now: at(0), echo: true });
    a.speechStopped({ itemId: "e1", now: at(300) });
    run(9000);
    eq(out.utterances.length, 1, "reported once");
    ok(out.utterances[0].echo && !out.utterances[0].text, "an empty echo — silent");
  },

  "the room holding 'speaking' open doesn't hold the sentence"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ itemId: "i1", now: at(0) });
    a.speechStopped({ itemId: "i1", now: at(900) });
    a.committed({ itemId: "i1", now: at(10) });
    a.completed({ itemId: "i1", transcript: "Which dogs are in tomorrow?", now: at(200) });
    a.speechStarted({ itemId: "fan", now: at(100) });      // a fan, never words
    run(3000);
    eq(out.utterances.length, 1, "sent without waiting for the fan");
    eq(out.utterances[0].text, "Which dogs are in tomorrow?", "the question");
    eq(out.utterances[0].why, "noise_hold", "why");
  },

  "events without ids fall on the segment being spoken"() {
    const { a, out, at, run } = rig();
    a.speechStarted({ now: at(0) });
    a.delta({ delta: "Hello", now: at(300) });
    a.speechStopped({ now: at(300) });
    a.committed({ now: at(10) });
    a.completed({ transcript: "Hello there", now: at(200) });
    run(SETTLE_MS + 200);
    eq(out.utterances.length, 1, "one");
    eq(out.utterances[0].text, "Hello there", "final text");
  },
};

function eq(got, want, what) {
  if (got !== want) throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}
function ok(cond, what) { if (!cond) throw new Error(what); }

let failed = 0;
for (const [name, fn] of Object.entries(scenarios)) {
  try { fn(); console.log("ok   " + name); }
  catch (e) { failed += 1; console.log("FAIL " + name + " — " + e.message); }
}
process.exit(failed ? 1 : 0);
