'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { description, categories, assessCI, report, sameRevision, publish, inspect, MARKER } = require('../../scripts/ci/pr_review.cjs');

const pr = () => ({ number: 2, state: 'open', title: 'Improve checks', draft: false, mergeable: true,
  body: '## Purpose\nCatch regressions.\n## Changes\nAdd a report.\n## Validation\nTests passed.\n## Risks and rollback\nRevert this commit.',
  head: { sha: 'head1' }, base: { sha: 'base1', ref: 'main', repo: { html_url: 'https://github.com/haail-dev/BLAM8' } },
  changed_files: 1, additions: 5, deletions: 2 });
const ciRun = (extra = {}) => ({ id: 10, event: 'pull_request', head_sha: 'head1', status: 'completed', conclusion: 'success',
  pull_requests: [{ number: 2, base: { sha: 'base1', ref: 'main' } }], ...extra });
const jobs = [{ name: 'offline', run_id: 10, status: 'completed', conclusion: 'success' }];
const files = [{ filename: 'docs/example.md', status: 'modified' }];
const goodChecks = [{ path: 'offline', ok: true, text: 'Passed' }, { path: 'review-tests', ok: true, text: 'Passed' }];

test('unfilled template comments do not count as a description', () => {
  assert.equal(description('## Purpose\n<!-- Explain why -->\n## Changes\n<!-- Explain what -->').missing.length, 4);
  assert.deepEqual(description(pr().body).missing, []);
});

test('accept only a successful required job for the same PR head and base', () => {
  assert.equal(assessCI(pr(), [ciRun()], jobs, 'offline').ok, true);
  for (const replacement of [
    { head_sha: 'older-head' },
    { event: 'push' },
    { pull_requests: [{ number: 99, base: { sha: 'base1', ref: 'main' } }] },
    { pull_requests: [{ number: 2, base: { sha: 'old-base', ref: 'main' } }] },
    { pull_requests: [{ number: 2, base: { sha: 'base1', ref: 'other' } }] },
  ]) assert.equal(assessCI(pr(), [ciRun(replacement)], jobs, 'offline').ok, false);
});

for (const conclusion of ['failure', 'cancelled', 'skipped', 'neutral', 'timed_out', null]) {
  test(`does not count ${conclusion} as passing CI`, () => {
    assert.equal(assessCI(pr(), [ciRun({ conclusion })], jobs, 'offline').ok, false);
  });
}

test('a newer pending rerun cannot borrow an older success', () => {
  const check = assessCI(pr(), [ciRun(), ciRun({ id: 11, status: 'queued', conclusion: null })], jobs, 'offline');
  assert.equal(check.ok, false);
  assert.equal(check.run.id, 11);
});

test('missing, skipped and wrong-run jobs cannot satisfy the required job', () => {
  for (const wrong of [[], [{ ...jobs[0], conclusion: 'skipped' }], [{ ...jobs[0], run_id: 9 }], [{ ...jobs[0], name: 'unrelated' }]])
    assert.equal(assessCI(pr(), [ciRun()], wrong, 'offline').ok, false);
});

test('rename from the exception register still receives sensitive-change warning', () => {
  const warnings = categories([{ filename: 'docs/old.json', previous_filename: 'tests/fixtures/agnostic/debt.json' }]);
  assert.ok(warnings.some(w => w.includes('exception register')));
});

test('detect deployment/database/credential paths without labelling env example a secret', () => {
  const warnings = categories([{ filename: 'render.yaml' }, { filename: 'db/migrations/x.sql' }, { filename: '.env.prod' }]);
  assert.equal(warnings.length, 3);
  assert.equal(categories([{ filename: '.env.example' }]).length, 0);
});

test('empty checks, incomplete diff, draft and unknown mergeability hold the decision', () => {
  assert.match(report(pr(), files, []), /HOLD/);
  for (const changed of [{ changed_files: 3001 }, { draft: true }, { mergeable: null }, { mergeable: false }])
    assert.match(report({ ...pr(), ...changed }, files, goodChecks), /HOLD/);
});

test('complete evidence still requires human review', () => {
  const body = report(pr(), files, goodChecks);
  assert.doesNotMatch(body, /HOLD/);
  assert.match(body, /human review still required/);
  assert.match(body, /never submits an approving review/);
});

test('PR text and filenames cannot inject headings, HTML, links or mentions', () => {
  const hostile = '<img src=x> @everyone [Approve](https://evil.invalid)\n# approved';
  const body = report({ ...pr(), body: pr().body.replace('Catch regressions.', hostile) }, [{ filename: hostile, status: 'added' }], goodChecks);
  assert.doesNotMatch(body, /<img|@everyone|\n# approved|\[Approve\]\(https/);
});

test('report truncates long file listings and describes the omitted files', () => {
  const many = Array.from({ length: 60 }, (_, i) => ({ filename: `docs/${i}.md`, status: 'added' }));
  const body = report({ ...pr(), changed_files: 60 }, many, goodChecks);
  assert.match(body, /20 additional files/);
  assert.doesNotMatch(body, /docs\/59/);
});

test('head/base changes, retargeting, edits and closed PRs invalidate evidence', () => {
  assert.equal(sameRevision(pr(), pr()), true);
  for (const changed of [{ head: { sha: 'new' } }, { base: { ...pr().base, sha: 'new' } },
    { base: { ...pr().base, ref: 'other' } }, { body: 'edited' }, { title: 'edited' }, { draft: true }, { state: 'closed' }])
    assert.equal(sameRevision(pr(), { ...pr(), ...changed }), false);
});

test('very long renamed paths stay within the GitHub comment size limit', () => {
  const longFiles = Array.from({ length: 40 }, () => ({ filename: '['.repeat(4000), previous_filename: '['.repeat(4000), status: 'renamed' }));
  assert.ok(report({ ...pr(), changed_files: 40 }, longFiles, goodChecks).length < 60000);
});

function fakeGitHub(current = pr(), comments = []) {
  const writes = [];
  return { writes, rest: {
    pulls: { get: async () => ({ data: current }) },
    issues: { listComments: 'comments', updateComment: async p => writes.push({ update: p }), createComment: async p => writes.push({ create: p }) },
  }, paginate: async () => comments };
}

test('a changed PR cannot receive a stale report', async () => {
  const github = fakeGitHub({ ...pr(), head: { sha: 'new' } });
  assert.equal(await publish({ github, repo: {}, evidence: { pr: pr(), files, checks: goodChecks } }), false);
  assert.equal(github.writes.length, 0);
});

test('update the bot report, never a user comment with a spoofed marker', async () => {
  const comments = [
    { id: 1, body: MARKER, user: { login: 'author', type: 'User' } },
    { id: 2, body: MARKER, user: { login: 'github-actions[bot]', type: 'Bot' } },
  ];
  const github = fakeGitHub(pr(), comments);
  await publish({ github, repo: {}, evidence: { pr: pr(), files, checks: goodChecks } });
  assert.equal(github.writes[0].update.comment_id, 2);
});

test('create one report when only a spoofed marker exists', async () => {
  const github = fakeGitHub(pr(), [{ id: 1, body: MARKER, user: { login: 'author', type: 'User' } }]);
  await publish({ github, repo: {}, evidence: { pr: pr(), files, checks: goodChecks } });
  assert.equal(github.writes[0].create.issue_number, 2);
});

test('API failures propagate instead of reporting missing checks as success', async () => {
  const github = { rest: { pulls: { get: async () => ({ data: pr() }), listFiles: 'files' }, actions: { listWorkflowRuns: 'runs' } },
    paginate: async endpoint => { if (endpoint === 'files') return files; throw Object.assign(new Error('Forbidden'), { status: 403 }); } };
  await assert.rejects(inspect({ github, repo: {}, number: 2 }), /Forbidden/);
  github.paginate = async endpoint => { if (endpoint === 'files') return files; throw Object.assign(new Error('Not found'), { status: 404 }); };
  const result = await inspect({ github, repo: {}, number: 2 });
  assert.ok(result.checks.every(c => !c.ok));
});
