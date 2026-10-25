const form = document.querySelector('#scan-form');
const status = document.querySelector('#status');
const report = document.querySelector('#report');
const button = document.querySelector('#scan-button');
let lastReport = null;

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined && content !== null) node.textContent = String(content);
  return node;
}

const detailLabels = {
  subtype: 'Smell subtype', error_type: 'Position error type', action: 'Cache action',
  key: 'Cache key', path: 'Cache path', cache_path: 'Cache path',
  resource_type: 'Cache resource types', resource_types: 'Cache resource types',
  restore_keys: 'Fallback keys', effective_restore_keys: 'Effective fallback keys',
  ineffective_restore_keys: 'Ineffective fallback keys',
  consumer: 'Cache consumer', producer: 'Cache producer',
  cache_step: 'Cache step', consumer_step: 'Consumer step', producer_step: 'Producer step',
};

function findingDetails(details) {
  if (!details || !Object.keys(details).length) return null;
  const panel = el('details', 'finding-details');
  panel.append(el('summary', '', 'Evidence and details'));
  const fields = el('dl');
  Object.entries(details).forEach(([key, value]) => {
    const text = value === null ? 'Not provided'
      : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
    fields.append(el('dt', '', detailLabels[key] || key), el('dd', '', text));
  });
  panel.append(fields);
  return panel;
}

function render(data) {
  lastReport = data;
  report.classList.remove('hidden');
  document.querySelector('#workflow-name').textContent = data.workflow_name;
  document.querySelector('#workflow-meta').textContent = `${data.repository} / ${data.workflow_path} · ${data.job_count} ${data.job_count === 1 ? 'job' : 'jobs'}`;
  document.querySelector('#finding-count').textContent = data.summary.total;
  document.querySelector('#type-count').textContent = data.summary.types;
  document.querySelector('#result-caption').textContent = `${data.findings.length} ${data.findings.length === 1 ? 'finding' : 'findings'}`;
  document.querySelector('#source-path').textContent = data.workflow_path;
  document.querySelector('#blob-sha').textContent = data.github_blob_sha || 'Not provided';
  document.querySelector('#content-sha').textContent = data.content_sha256;
  const link = document.querySelector('#source-link');
  if (data.source_url && data.source_url.startsWith('https://github.com/')) {
    link.href = data.source_url;
    link.hidden = false;
  } else {
    link.removeAttribute('href');
    link.hidden = true;
  }

  const rules = document.querySelector('#rule-list');
  rules.replaceChildren();
  data.rules.forEach(rule => {
    const row = el('div', `rule-row${rule.count ? ' active' : ''}`);
    row.append(el('span', '', rule.label), el('span', 'rule-count', rule.count));
    row.title = `${rule.type}: ${rule.description}`;
    rules.append(row);
  });

  const list = document.querySelector('#findings');
  list.replaceChildren();
  if (!data.findings.length) {
    const empty = el('div', 'empty');
    empty.append(el('strong', '', 'No cache smells detected'), el('span', '', 'All 11 smell checks completed without findings. Review your workflow and runtime behavior for additional context.'));
    list.append(empty);
  }
  data.findings.forEach(item => {
    const card = el('article', 'finding');
    const head = el('div', 'finding-head');
    head.append(el('h4', '', item.label));
    const location = [item.job && `Job: ${item.job}`, item.step && `Step: ${item.step}`, item.line && `Line ${item.line}`].filter(Boolean).join(' · ') || 'Workflow-level finding';
    const advice = el('div', 'suggestion');
    advice.append(el('strong', '', 'Suggested fix  '), document.createTextNode(item.suggestion));
    card.append(head, el('div', 'location', location), el('p', '', item.message || item.description), advice);
    const details = findingDetails(item.details);
    if (details) card.append(details);
    list.append(card);
  });
  report.scrollIntoView({behavior:'smooth', block:'start'});
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  status.textContent = 'Fetching the workflow from GitHub and scanning…';
  status.classList.remove('error');
  button.disabled = true;
  report.classList.add('hidden');
  try {
    const response = await fetch('/api/scan', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({repository:document.querySelector('#repository').value, workflow_path:document.querySelector('#workflow-path').value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Scan failed.');
    status.textContent = `Scan complete: ${data.summary.total} ${data.summary.total === 1 ? 'finding' : 'findings'}.`;
    render(data);
  } catch (error) {
    status.classList.add('error');
    status.textContent = error.message || 'Connection failed.';
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#download').addEventListener('click', () => {
  if (!lastReport) return;
  const blob = new Blob([JSON.stringify(lastReport, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${lastReport.repository.replace('/', '-')}-${lastReport.workflow_path.split('/').pop()}-scan.json`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
