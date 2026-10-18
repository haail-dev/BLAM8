'use strict';

// Executed only from the default branch by the privileged reporting workflow.
// PR text, filenames and check results are data; no PR modules or artifacts run.
const MARKER = '<!-- blam8-pr-review:v1 -->';
const SECTIONS = ['Purpose', 'Changes', 'Validation', 'Risks and rollback'];
const WORKFLOWS = [
  { path: '.github/workflows/offline-tests.yml', job: 'Platform tests and agnostic guard' },
  { path: '.github/workflows/review-automation-tests.yml', job: 'review-tests' },
];

function safe(value) {
  return String(value).replace(/[\r\n]+/g, ' ').replace(/&/g, '&amp;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/@/g, '@\u200b')
    .replace(/[\\`*_{}[\]()#!|~]/g, '\\$&');
}

function description(body = '') {
  const clean = (body || '').replace(/<!--[\s\S]*?-->/g, '');
  const sections = {};
  for (const part of clean.split(/^##\s+/m).slice(1)) {
    const [heading, ...lines] = part.split('\n');
    sections[heading.trim().toLowerCase()] = lines.join('\n').trim();
  }
  const missing = SECTIONS.filter(name => !sections[name.toLowerCase()]);
  return { missing, purpose: sections.purpose || '' };
}

function categories(files) {
  const groups = new Set();
  for (const f of files) {
    // Inspect both sides of renames, so moving a protected file does not hide it.
    for (const path of [f.filename, f.previous_filename].filter(Boolean)) {
      if (/^tests\/fixtures\/agnostic\//.test(path)) groups.add('Agnostic snapshots or exception register: review every changed record; never bulk-refresh to pass.');
      if (/^(\.github\/|scripts\/ci\/|tests\/(?:agnostic|test_agnostic|run_offline))/.test(path)) groups.add('CI or guard implementation: inspect for weaker checks, skips and broader token permissions.');
      if (/^(db\/|scripts\/db\/)/.test(path)) groups.add('Database: review data effects, migration ordering and rollback compatibility.');
      if (/^(render\.yaml$|deploy\/)/.test(path)) groups.add('Deployment: review live-service effects and release/rollback steps.');
      if (/^src\//.test(path)) groups.add('Runtime source: verify intended behaviour and relevant regression coverage.');
      if (/(^|\/)(requirements[^/]*\.txt|package(?:-lock)?\.json|pyproject\.toml)$/.test(path)) groups.add('Dependencies: review added packages, versions and build compatibility.');
      if (/(^|\/)(\.env(?:\..*)?|[^/]+\.(?:pem|key))$/.test(path) && !path.endsWith('.env.example')) groups.add('Possible credential/configuration file: inspect for secrets before merging. This report is not a secret scanner.');
    }
  }
  return [...groups];
}

function assessCI(pr, runs, jobs, jobName) {
  // Never fall back to an older successful run when a newer run is pending/failed.
  const run = runs.filter(r => r.event === 'pull_request' && r.head_sha === pr.head.sha
    && r.pull_requests?.some(p => p.number === pr.number))
    .sort((a, b) => b.id - a.id)[0];
  if (!run) return { ok: false, text: 'No PR run for the current head.' };
  const association = run.pull_requests.find(p => p.number === pr.number);
  if (association.base.sha !== pr.base.sha || association.base.ref !== pr.base.ref)
    return { ok: false, text: 'Stale: CI tested a different base commit or branch.', run };
  if (run.status !== 'completed') return { ok: false, text: `Pending: ${run.status}.`, run };
  if (run.conclusion !== 'success') return { ok: false, text: `Not passed: ${run.conclusion || 'unknown'}.`, run };
  const required = jobs.filter(j => j.name === jobName && j.run_id === run.id);
  if (required.length !== 1 || required[0].status !== 'completed' || required[0].conclusion !== 'success')
    return { ok: false, text: `Required job ${jobName} did not complete successfully.`, run };
  return { ok: true, text: 'Passed for this head and base.', run };
}

function report(pr, files, checks, generatedAt = new Date().toISOString()) {
  const desc = description(pr.body);
  const attention = categories(files);
  const holds = [];
  if (desc.missing.length) holds.push(`Complete description sections: ${desc.missing.join(', ')}.`);
  if (files.length !== pr.changed_files) holds.push('File listing is incomplete; inspect the full diff manually.');
  if (pr.draft) holds.push('PR is a draft.');
  if (pr.mergeable !== true) holds.push(pr.mergeable === false ? 'Resolve merge conflicts.' : 'GitHub has not yet established mergeability; refresh the report.');
  if (checks.length !== WORKFLOWS.length || checks.some(c => !c.ok)) holds.push('Wait for both configured CI workflows to pass on the current head and base.');
  const pathLabel = path => safe(path.length > 180 ? `${path.slice(0, 180)}…` : path);
  const paths = files.slice(0, 40).map(f => `- ${safe(f.status)}: ${pathLabel(f.filename)}${f.previous_filename ? ` (from ${pathLabel(f.previous_filename)})` : ''}`);
  if (files.length > 40) paths.push(`- ${files.length - 40} additional files: open Files changed.`);
  return [MARKER, '## Automated review evidence',
    `Head: \`${pr.head.sha}\` · Base: \`${pr.base.sha}\` (${safe(pr.base.ref)})`,
    `Generated: ${generatedAt}. This report is a snapshot; recheck the PR after new commits or reruns.`,
    '### 1. Purpose',
    desc.purpose ? `Author-stated purpose: ${safe(desc.purpose.slice(0, 600))}` : 'Purpose section is missing.',
    desc.missing.length ? `Missing sections: ${desc.missing.join(', ')}.` : 'All four description sections contain text. A person must judge whether the explanation is accurate.',
    '### 2. Scope', `${pr.changed_files} files; +${pr.additions} / -${pr.deletions} lines.`, ...paths,
    '### 3. Protections and sensitive changes',
    ...(attention.length ? attention.map(a => `- ${a}`) : ['No configured sensitive-path patterns matched. This does not prove the change is safe.']),
    'File patterns identify review areas; this workflow does not judge code correctness or approve snapshot changes.',
    '### 4. Latest CI evidence',
    ...checks.map(c => `- ${safe(c.path)}: **${safe(c.text)}**${c.run ? ` [Run ${c.run.id}](${pr.base.repo.html_url}/actions/runs/${c.run.id})` : ''}`),
    'Only completed success counts. Skipped, neutral, cancelled and missing results do not pass this report.',
    '### 5. Merge decision',
    holds.length ? '**HOLD — evidence is incomplete.**' : '**Evidence complete — human review still required.**',
    ...holds.map(h => `- ${h}`),
    'Review the actual diff, sensitive areas, behaviour and rollback plan. This bot never submits an approving review, enables auto-merge or merges a PR.',
    'The reporting job succeeding means the report was published, not that the PR is approved. On GitHub Free private repositories this report cannot enforce a merge restriction.',
  ].join('\n\n');
}

function sameRevision(a, b) {
  return b.state === 'open' && a.head.sha === b.head.sha && a.base.sha === b.base.sha
    && a.base.ref === b.base.ref && a.title === b.title && a.body === b.body && a.draft === b.draft;
}

async function inspect({ github, repo, number }) {
  const { data: pr } = await github.rest.pulls.get({ ...repo, pull_number: number });
  const files = await github.paginate(github.rest.pulls.listFiles, { ...repo, pull_number: number, per_page: 100 });
  const checks = [];
  for (const spec of WORKFLOWS) {
    let runs;
    try {
      runs = await github.paginate(github.rest.actions.listWorkflowRuns, {
        ...repo, workflow_id: spec.path.split('/').pop(), event: 'pull_request', head_sha: pr.head.sha, per_page: 100,
      });
    } catch (error) {
      if (error.status !== 404) throw error;
      checks.push({ ...spec, ok: false, text: 'Workflow is not available yet.' });
      continue;
    }
    let check = assessCI(pr, runs, [], spec.job);
    if (check.run?.status === 'completed' && check.run.conclusion === 'success') {
      const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
        ...repo, run_id: check.run.id, filter: 'latest', per_page: 100,
      });
      check = assessCI(pr, runs, jobs, spec.job);
    }
    checks.push({ ...spec, ...check });
  }
  return { pr, files, checks };
}

async function publish({ github, repo, evidence }) {
  const { pr } = evidence;
  const comments = await github.paginate(github.rest.issues.listComments, {
    ...repo, issue_number: pr.number, per_page: 100,
  });
  // A force-push, edit, retarget or closure while API calls were in flight must
  // not cause a stale report to overwrite the newer PR's evidence.
  const { data: current } = await github.rest.pulls.get({ ...repo, pull_number: pr.number });
  if (!sameRevision(pr, current)) return false;
  const existing = comments.find(c => c.user?.login === 'github-actions[bot]'
    && c.user.type === 'Bot' && c.body?.startsWith(MARKER));
  const body = report(current, evidence.files, evidence.checks);
  if (existing) await github.rest.issues.updateComment({ ...repo, comment_id: existing.id, body });
  else await github.rest.issues.createComment({ ...repo, issue_number: pr.number, body });
  return true;
}

async function run({ github, context, core }) {
  const repo = context.repo;
  const prs = await github.paginate(github.rest.pulls.list, { ...repo, state: 'open', per_page: 100 });
  for (const pr of prs) {
    const evidence = await inspect({ github, repo, number: pr.number });
    const written = await publish({ github, repo, evidence });
    core.info(`PR #${pr.number}: ${written ? 'review evidence updated' : 'changed during inspection; report withheld'}`);
  }
}

module.exports = { MARKER, WORKFLOWS, description, categories, assessCI, report, sameRevision, inspect, publish, run };
