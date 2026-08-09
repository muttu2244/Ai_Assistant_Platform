function showView(view) {
  document.getElementById('homeView').classList.toggle('active', view === 'home');
  document.getElementById('keywordView').classList.toggle('active', view === 'keyword');
  document.getElementById('structuredView').classList.toggle('active', view === 'structured');
  document.getElementById('freetextView').classList.toggle('active', view === 'freetext');
  document.getElementById('goFreeTextHome').classList.toggle('active', view === 'freetext');
  document.getElementById('goKeywordHome').classList.toggle('active', view === 'keyword');
  document.getElementById('goStructuredHome').classList.toggle('active', view === 'structured');
}

var predictiveCandidatesAll = [];
var predictiveLastRunMeta = null;
var predictiveLastRunType = '';
var CHAT_STORAGE_KEY = 'smartcare_chat_sessions_v1';
var CHAT_CONTEXT_STORAGE_KEY = 'smartcare_chat_context_v1';
var chatSessions = [];
var currentChatId = null;
var chatContextBySession = {};

function getTopScoreLevelsValue() {
  var raw = String((document.getElementById('pfTopLevels') || {}).value || '50').trim();
  var parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    parsed = 50;
  }
  parsed = Math.max(1, Math.min(500, Math.floor(parsed)));
  return parsed;
}

function setPredictiveBusyCursor(isBusy) {
  document.body.style.cursor = isBusy ? 'progress' : '';
}

function stageLabel(step) {
  var key = String(step || '').toLowerCase();
  var labels = {
    validation: 'Validating Inputs',
    msp_feature_module_mapper: 'MSP Mapping',
    ado_export: 'ADO Export',
    combine_mapping: 'Combine Mapping',
    load_inputs: 'Load Inputs',
    predictive_scoring: 'Predictive Scoring',
    finalizing: 'Finalizing',
    completed: 'Completed',
    failed: 'Failed'
  };
  return labels[key] || (step || 'Running');
}

function formatRunProgressText(run) {
  if (!run || typeof run !== 'object') {
    return 'Running pipeline...';
  }
  var state = String(run.state || 'running').toLowerCase();
  var step = stageLabel(run.step);
  var msg = String(run.message || '').trim();
  if (state === 'failed') {
    return 'Failed at ' + step + (msg ? ': ' + msg : '');
  }
  if (state === 'completed') {
    return 'Completed: ' + (msg || 'Pipeline completed successfully');
  }
  return '[Running] ' + step + (msg ? ' - ' + msg : '');
}

function startRunStatusPolling(runId, statusEl) {
  if (!runId || !statusEl) {
    return null;
  }
  return setInterval(async function() {
    try {
      var res = await fetch('/api/predictive/run-status?run_id=' + encodeURIComponent(runId));
      if (!res.ok) {
        return;
      }
      var run = await res.json();
      statusEl.textContent = formatRunProgressText(run);
    } catch (_) {}
  }, 1200);
}

function renderModuleBars(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return '<div class="muted">No module data.</div>';
  }
  var maxVal = rows.reduce(function(max, item) { return Math.max(max, Number(item.ticket_count || 0)); }, 1);
  return rows.map(function(item) {
    var pct = Math.round((Number(item.ticket_count || 0) / maxVal) * 100);
    return '<div class="bar-row"><div><div class="strong small">' + (item.module_name || '-') + '</div><div class="bar-track"><div class="bar-fill" style="width:' + pct + '%"></div></div></div><div class="strong">' + String(item.ticket_count || 0) + '</div></div>';
  }).join('');
}

function renderFuncBars(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return '<div class="muted">No functionality data.</div>';
  }
  var maxVal = rows.reduce(function(max, item) { return Math.max(max, Number(item.ticket_count || 0)); }, 1);
  return rows.map(function(item) {
    var pct = Math.round((Number(item.ticket_count || 0) / maxVal) * 100);
    return '<div class="bar-row"><div><div class="strong small">' + (item.functionality || '-') + '</div><div class="small muted">' + (item.module_name || '') + '</div><div class="bar-track"><div class="bar-fill" style="width:' + pct + '%;background:linear-gradient(90deg,#ff8c42,#ef4e6c);"></div></div></div><div class="strong">' + String(item.ticket_count || 0) + '</div></div>';
  }).join('');
}

function shouldShowDependencyColumns(rows) {
  if (!Array.isArray(rows) || !rows.length) {
    return true;
  }
  return rows.some(function(item) {
    var relationship = String(item.relationship_type || '').toLowerCase();
    var distance = Number(item.dependency_distance || 0);
    return relationship !== 'direct_change' || distance !== 0;
  });
}

function setCandidateDependencyColumnsVisible(isVisible) {
  var relationHead = document.getElementById('pfCandidateRelationshipHead');
  var hopHead = document.getElementById('pfCandidateHopHead');
  if (relationHead) relationHead.style.display = isVisible ? '' : 'none';
  if (hopHead) hopHead.style.display = isVisible ? '' : 'none';
}

var predictiveMlScores = {};

function renderCandidateRows(rows) {
  var showDependencyColumns = shouldShowDependencyColumns(rows);
  setCandidateDependencyColumnsVisible(showDependencyColumns);
  if (!Array.isArray(rows) || !rows.length) {
    return '<tr><td colspan="' + String(showDependencyColumns ? 10 : 8) + '" class="muted">No recurrence candidates found.</td></tr>';
  }
  return rows.map(function(item, idx) {
    var risk = String(item.risk_level || 'None');
    var cls = risk === 'High' ? 'row-risk-high' : (risk === 'Medium' ? 'row-risk-medium' : (risk === 'Low' ? 'row-risk-low' : ''));
    var preview = String(item.ticket_ids_preview || '-');
    var allIds = String(item.ticket_ids_all || preview);
    var moreCount = Number(item.ticket_more_count || 0);
    var ticketCell = preview;
    if (moreCount > 0) {
      var expandedId = 'tickets-expanded-' + idx;
      var toggleId = 'tickets-toggle-' + idx;
      ticketCell += ' <a href="#" id="' + toggleId + '" onclick="return toggleTicketIds(\'' + expandedId + '\', this)">+' + String(moreCount) + ' more</a>';
      ticketCell += '<div id="' + expandedId + '" style="display:none;margin-top:4px;font-size:11px;color:#51607a;">' + allIds + '</div>';
    }
    var relationCell = showDependencyColumns ? '<td><span class="pill">' + (item.relationship_type || '-') + '</span></td><td>' + String(item.dependency_distance || '-') + '</td>' : '';
    var mlScore = predictiveMlScores[String(idx)];
    var aiRiskCell;
    if (!mlScore) {
      aiRiskCell = '<td><span class="muted">-</span></td>';
    } else {
      var score = Number(mlScore.risk_score || 0);
      var pct = Math.round(score * 100);
      var badgeCls, badgeLabel;
      if (mlScore.predicted_label === 1) {
        badgeCls = 'badge-risk-high'; badgeLabel = 'High (' + pct + '%)';
      } else if (score >= 0.30) {
        badgeCls = 'badge-risk-medium'; badgeLabel = 'Med (' + pct + '%)';
      } else {
        badgeCls = 'badge-risk-low'; badgeLabel = 'Low (' + pct + '%)';
      }
      aiRiskCell = '<td><span class="badge-risk ' + badgeCls + '">' + badgeLabel + '</span></td>';
    }
    return '<tr class="' + cls + '"><td>' + String(item.impact_score || '-') + '</td><td>' + String(item.ticket_count || 0) + '</td><td>' + ticketCell + '</td>' + relationCell + '<td>' + (item.module_name || '-') + '</td><td>' + (item.modified_functionality || '-') + '</td><td>' + (item.impact_reason || '-') + '</td><td>' + risk + '</td>' + aiRiskCell + '</tr>';
  }).join('');
}

function getPredictiveFilterValues() {
  return {
    moduleFilter: String((document.getElementById('pfModuleFilter') || {}).value || '').trim().toLowerCase(),
    functionFilter: String((document.getElementById('pfFunctionFilter') || {}).value || '').trim().toLowerCase()
  };
}

function getFilteredPredictiveCandidates() {
  if (!Array.isArray(predictiveCandidatesAll) || !predictiveCandidatesAll.length) {
    return [];
  }
  var filters = getPredictiveFilterValues();
  return predictiveCandidatesAll.filter(function(item) {
    var moduleName = String(item.module_name || '').trim().toLowerCase();
    var modifiedFunction = String(item.modified_functionality || '').trim().toLowerCase();
    if (filters.moduleFilter && moduleName !== filters.moduleFilter) return false;
    if (filters.functionFilter && modifiedFunction !== filters.functionFilter) return false;
    return true;
  });
}

function repopulatePredictiveFunctionDropdown(previousValue) {
  var moduleEl = document.getElementById('pfModuleFilter');
  var functionEl = document.getElementById('pfFunctionFilter');
  if (!moduleEl || !functionEl) return;
  var selectedModule = String(moduleEl.value || '').trim().toLowerCase();
  var functionNames = Array.from(new Set(
    predictiveCandidatesAll.filter(function(item) {
      if (!selectedModule) return true;
      return String(item.module_name || '').trim().toLowerCase() === selectedModule;
    }).map(function(item) {
      return String(item.modified_functionality || '').trim();
    }).filter(Boolean)
  )).sort(function(a, b) { return a.localeCompare(b); });
  functionEl.innerHTML = '<option value="">All Functions</option>' + functionNames.map(function(name) {
    return '<option value="' + name.replace(/"/g, '&quot;') + '">' + name + '</option>';
  }).join('');
  functionEl.value = previousValue && functionNames.indexOf(previousValue) !== -1 ? previousValue : '';
}

function repopulatePredictiveFilterDropdowns() {
  var moduleEl = document.getElementById('pfModuleFilter');
  var functionEl = document.getElementById('pfFunctionFilter');
  if (!moduleEl || !functionEl) return;
  var prevModule = String(moduleEl.value || '');
  var prevFunction = String(functionEl.value || '');
  var moduleNames = Array.from(new Set(predictiveCandidatesAll.map(function(item) {
    return String(item.module_name || '').trim();
  }).filter(Boolean))).sort(function(a, b) { return a.localeCompare(b); });
  moduleEl.innerHTML = '<option value="">All Modules</option>' + moduleNames.map(function(name) {
    return '<option value="' + name.replace(/"/g, '&quot;') + '">' + name + '</option>';
  }).join('');
  moduleEl.value = prevModule && moduleNames.indexOf(prevModule) !== -1 ? prevModule : '';
  repopulatePredictiveFunctionDropdown(prevFunction);
}

function updatePredictiveStatusFromCurrentView() {
  var statusEl = document.getElementById('pfStatus');
  if (!statusEl || !predictiveLastRunMeta) return;
  var filteredRows = getFilteredPredictiveCandidates();
  var selectedLevels = getTopScoreLevelsValue();
  var shownRows = Math.min(selectedLevels, filteredRows.length || 0);
  var moduleLabel = String((document.getElementById('pfModuleFilter') || {}).value || '').trim() || 'All';
  var functionLabel = String((document.getElementById('pfFunctionFilter') || {}).value || '').trim() || 'All';
  if (predictiveLastRunType === 'e2e') {
    statusEl.textContent = 'Completed: ' + (predictiveLastRunMeta.ran_at || 'now') + ' | release: ' + (predictiveLastRunMeta.release_name || 'MSP End-to-End Run') + ' | Viewing Levels: ' + String(selectedLevels) + ' | Available Levels: ' + String(filteredRows.length || 0) + ' | Returned Rows: ' + String(shownRows) + ' | Module: ' + moduleLabel + ' | Function: ' + functionLabel;
    return;
  }
  statusEl.textContent = 'Completed: ' + (predictiveLastRunMeta.ran_at || 'now') + ' | Match mode: ' + (predictiveLastRunMeta.match_mode || 'strict') + ' | Viewing Levels: ' + String(selectedLevels) + ' | Available Levels: ' + String(filteredRows.length || 0) + ' | Returned Rows: ' + String(shownRows) + ' | Module: ' + moduleLabel + ' | Function: ' + functionLabel;
}

function applyPredictiveTopLevels() {
  var body = document.getElementById('pfCandidateRows');
  if (!Array.isArray(predictiveCandidatesAll) || !predictiveCandidatesAll.length) {
    body.innerHTML = renderCandidateRows([]);
    updatePredictiveStatusFromCurrentView();
    return;
  }
  var selectedLevels = getTopScoreLevelsValue();
  var filteredRows = getFilteredPredictiveCandidates();
  body.innerHTML = renderCandidateRows(filteredRows.slice(0, selectedLevels));
  updatePredictiveStatusFromCurrentView();
}

async function fetchAndMergeMLScores(candidates) {
  if (!Array.isArray(candidates) || !candidates.length) return;
  var tickets = candidates.map(function(item, idx) {
    return {
      ticket_id: String(idx),
      module_name: item.module_name || null,
      modified_functionality: item.modified_functionality || null,
      customer_priority: (item.risk_level === 'High') ? 'high' : (item.risk_level === 'Medium') ? 'medium' : 'low',
      work_item_type: 'Bug'
    };
  });
  try {
    var res = await fetch('/api/predictive/ml-score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tickets: tickets })
    });
    if (!res.ok) return;
    var data = await res.json();
    var newScores = {};
    (data.scores || []).forEach(function(s) { newScores[String(s.ticket_id)] = s; });
    predictiveMlScores = newScores;
    applyPredictiveTopLevels();
  } catch (err) {
    console.warn('ML scoring unavailable:', err);
  }
}

function toggleTicketIds(containerId, linkEl) {
  var el = document.getElementById(containerId);
  if (!el) return false;
  var isHidden = (el.style.display === 'none' || !el.style.display);
  if (isHidden) {
    el.style.display = 'block';
    if (linkEl) linkEl.textContent = 'show less';
  } else {
    el.style.display = 'none';
    if (linkEl) {
      var hiddenCount = (el.textContent || '').split(';').length;
      var more = Math.max(0, hiddenCount - 5);
      linkEl.textContent = '+' + String(more) + ' more';
    }
  }
  return false;
}

async function runPredictiveDefaults() {
  var statusEl = document.getElementById('pfStatus');
  var runBtn = document.getElementById('pfRunBtn');
  var runE2EBtn = document.getElementById('pfRunE2EBtn');
  statusEl.textContent = 'Running default predictive flow...';
  runBtn.disabled = true;
  if (runE2EBtn) runE2EBtn.disabled = true;
  try {
    var formData = new FormData();
    var topLevels = getTopScoreLevelsValue();
    formData.append('release_name', document.getElementById('pfRelease').value || 'MSP Default Run');
    formData.append('days', document.getElementById('pfDays').value || '180');
    formData.append('top_score_levels', String(topLevels));
    formData.append('work_item_scope', document.getElementById('pfScope').value || 'Bug,Customer Ticket');
    formData.append('priority_scope', document.getElementById('pfPriority').value || 'On Fire,Urgent,High');
    formData.append('match_mode', document.getElementById('pfMatch').value || 'strict');
    formData.append('include_dependencies', String(document.getElementById('pfDeps').value === 'true'));
    var modifiedFile = document.getElementById('pfModifiedFile').files[0];
    if (modifiedFile) formData.append('modified_functions_csv', modifiedFile);
    var dependencyFile = document.getElementById('pfDependencyFile').files[0];
    if (dependencyFile) formData.append('dependency_metrics_csv', dependencyFile);
    var res = await fetch('/api/predictive/run-defaults', { method: 'POST', body: formData });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Predictive flow failed');
    hydratePredictiveResults(data, topLevels, 'default');
    var outputs = data.outputs || {};
    document.getElementById('pfOutputHint').textContent = 'Output CSVs: ' + [outputs.recurrence_csv, outputs.module_summary_csv, outputs.functionality_summary_csv].filter(Boolean).join(' | ');
  } catch (err) {
    statusEl.textContent = 'Error: ' + String(err);
  } finally {
    runBtn.disabled = false;
    if (runE2EBtn) runE2EBtn.disabled = false;
  }
}

async function runPredictiveE2E() {
  var statusEl = document.getElementById('pfStatus');
  var runBtn = document.getElementById('pfRunBtn');
  var runE2EBtn = document.getElementById('pfRunE2EBtn');
  var mspInput = document.getElementById('pfMspFile');
  var mspFile = mspInput.files[0];
  if (!mspFile) {
    statusEl.textContent = 'Please select MSP workbook (.xlsx/.xlsm/.xls). Opening file picker...';
    if (mspInput) mspInput.click();
    return;
  }
  statusEl.textContent = '[Running] Initializing end-to-end pipeline...';
  runBtn.disabled = true;
  runE2EBtn.disabled = true;
  setPredictiveBusyCursor(true);
  var runId = 'e2e-' + String(Date.now()) + '-' + String(Math.floor(Math.random() * 100000));
  var statusPollTimer = null;
  try {
    var formData = new FormData();
    var topLevels = getTopScoreLevelsValue();
    formData.append('release_name', document.getElementById('pfRelease').value || 'MSP End-to-End Run');
    formData.append('days', document.getElementById('pfDays').value || '180');
    formData.append('top_score_levels', String(topLevels));
    formData.append('work_item_scope', document.getElementById('pfScope').value || 'Bug,Customer Ticket');
    formData.append('priority_scope', document.getElementById('pfPriority').value || 'On Fire,Urgent,High');
    formData.append('match_mode', document.getElementById('pfMatch').value || 'strict');
    formData.append('include_dependencies', String(document.getElementById('pfDeps').value === 'true'));
    formData.append('refresh_ado_export', String(document.getElementById('pfRefreshAdo').value === 'true'));
    formData.append('apply_query_update', String(document.getElementById('pfApplyQuery').value === 'true'));
    formData.append('msp_sheet_name', document.getElementById('pfMspSheet').value || '6.0_1-AprilMSP_2026');
    formData.append('msp_target_category', document.getElementById('pfMspCategory').value || 'Engineering Improvement Initiatives- NBL(I)');
    formData.append('run_id', runId);
    formData.append('msp_workbook', mspFile);
    var modifiedFile = document.getElementById('pfModifiedFile').files[0];
    if (modifiedFile) formData.append('modified_functions_csv', modifiedFile);
    var dependencyFile = document.getElementById('pfDependencyFile').files[0];
    if (dependencyFile) formData.append('dependency_metrics_csv', dependencyFile);
    statusPollTimer = startRunStatusPolling(runId, statusEl);
    var res = await fetch('/api/predictive/run-e2e', { method: 'POST', body: formData });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'End-to-end predictive flow failed');
    hydratePredictiveResults(data, topLevels, 'e2e');
    var outputs = data.outputs || {};
    document.getElementById('pfOutputHint').textContent = 'Output CSVs: ' + [outputs.ticket_export_csv, outputs.feature_module_csv, outputs.module_summary_csv, outputs.functionality_summary_csv, outputs.recurrence_csv].filter(Boolean).join(' | ');
  } catch (err) {
    statusEl.textContent = 'Error: ' + String(err);
  } finally {
    if (statusPollTimer) clearInterval(statusPollTimer);
    setPredictiveBusyCursor(false);
    runBtn.disabled = false;
    runE2EBtn.disabled = false;
  }
}

function hydratePredictiveResults(data, topLevels, runType) {
  var cards = data.cards || {};
  document.getElementById('kpiModules').textContent = String(cards.total_modules || 0);
  document.getElementById('kpiFuncs').textContent = String(cards.total_functionalities || 0);
  document.getElementById('kpiModified').textContent = String(cards.total_modified_functions || 0);
  document.getElementById('kpiCandidates').textContent = String(cards.candidate_rows || 0);
  document.getElementById('kpiHighRisk').textContent = String(cards.high_risk_rows || 0);
  document.getElementById('pfTopModules').innerHTML = renderModuleBars(data.top_modules || []);
  document.getElementById('pfTopFuncs').innerHTML = renderFuncBars(data.top_functionalities || []);
  var runMeta = data.run_meta || {};
  document.getElementById('pfTopLevels').value = String(Number(runMeta.top_score_levels || topLevels));
  predictiveCandidatesAll = Array.isArray(data.recurrence_candidates_all) ? data.recurrence_candidates_all : (data.recurrence_candidates || []);
  predictiveLastRunMeta = runMeta;
  predictiveLastRunType = runType;
  predictiveMlScores = {};
  repopulatePredictiveFilterDropdowns();
  applyPredictiveTopLevels();
  fetchAndMergeMLScores(predictiveCandidatesAll);
}

function newSessionTitle() { return 'New QA Chat'; }
function truncateSessionTitle(text) {
  var clean = (text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return newSessionTitle();
  return clean.length > 46 ? clean.slice(0, 46) + '...' : clean;
}
function createChatSession(initialTitle) {
  return { id: String(Date.now()) + '-' + String(Math.floor(Math.random() * 100000)), title: initialTitle || newSessionTitle(), messages: [], createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() };
}
function saveChatSessions() { try { localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(chatSessions)); } catch (err) { console.warn('Unable to persist chat sessions:', err); } }
function loadChatSessions() {
  try {
    var raw = localStorage.getItem(CHAT_STORAGE_KEY); if (!raw) return [];
    var parsed = JSON.parse(raw); if (!Array.isArray(parsed)) return [];
    return parsed.map(function(session) { return { id: session.id, title: session.title || newSessionTitle(), messages: Array.isArray(session.messages) ? session.messages : [], createdAt: session.createdAt || new Date().toISOString(), updatedAt: session.updatedAt || new Date().toISOString() }; });
  } catch (err) { console.warn('Unable to load chat sessions:', err); return []; }
}
function saveChatContextState() { try { localStorage.setItem(CHAT_CONTEXT_STORAGE_KEY, JSON.stringify(chatContextBySession)); } catch (err) { console.warn('Unable to persist chat context state:', err); } }
function loadChatContextState() {
  try {
    var raw = localStorage.getItem(CHAT_CONTEXT_STORAGE_KEY); if (!raw) return {};
    var parsed = JSON.parse(raw); return (!parsed || typeof parsed !== 'object') ? {} : parsed;
  } catch (err) { console.warn('Unable to load chat context state:', err); return {}; }
}
function getCurrentSession() { return chatSessions.find(function(session) { return session.id === currentChatId; }) || null; }
function renderChatHistory() {
  var host = document.getElementById('chatHistory');
  var searchEl = document.getElementById('chatHistorySearch');
  var query = searchEl ? String(searchEl.value || '').trim().toLowerCase() : '';
  if (!host) return;
  host.innerHTML = '';
  if (!chatSessions.length) { var empty = document.createElement('div'); empty.className = 'chat-item'; empty.style.opacity = '0.75'; empty.textContent = 'No chat sessions yet.'; host.appendChild(empty); return; }
  var sortedSessions = chatSessions.slice().sort(function(a, b) { return String(b.updatedAt).localeCompare(String(a.updatedAt)); });
  var visibleSessions = sortedSessions.filter(function(session) { if (!query) return true; return String(session.title || newSessionTitle()).toLowerCase().indexOf(query) !== -1; });
  if (!visibleSessions.length) { var noMatch = document.createElement('div'); noMatch.className = 'chat-item'; noMatch.style.opacity = '0.75'; noMatch.textContent = 'No matching chats.'; host.appendChild(noMatch); return; }
  visibleSessions.forEach(function(session) { var item = document.createElement('div'); item.className = 'chat-item' + (session.id === currentChatId ? ' active' : ''); item.textContent = session.title || newSessionTitle(); item.title = session.title || newSessionTitle(); item.onclick = function() { selectChatSession(session.id); }; host.appendChild(item); });
}
function getCurrentContextState() {
  if (!currentChatId) return null;
  if (!chatContextBySession[currentChatId]) { chatContextBySession[currentChatId] = { sessionId: null, uploadedDocuments: [] }; saveChatContextState(); }
  return chatContextBySession[currentChatId];
}
function renderAttachedFiles() {
  var host = document.getElementById('attachedFiles');
  var clearBtn = document.getElementById('clearAttachedBtn');
  if (!host) return;
  var state = getCurrentContextState();
  if (!state || !state.uploadedDocuments || !state.uploadedDocuments.length) { host.innerHTML = ''; if (clearBtn) clearBtn.classList.add('hidden'); return; }
  if (clearBtn) clearBtn.classList.remove('hidden');
  host.innerHTML = '';
  var label = document.createElement('span'); label.textContent = 'Attached: '; host.appendChild(label);
  state.uploadedDocuments.forEach(function(doc) {
    var chip = document.createElement('span');
    chip.className = 'chat-meta-pill';
    chip.style.margin = '0 6px 6px 0';
    chip.textContent = doc.filename || 'uploaded_file';
    var closeBtn = document.createElement('button'); closeBtn.type = 'button'; closeBtn.textContent = ' ×'; closeBtn.style.border = 'none'; closeBtn.style.background = 'transparent'; closeBtn.style.cursor = 'pointer'; closeBtn.onclick = function() { removeAttachedDocument(String(doc.document_id || '')); };
    chip.appendChild(closeBtn);
    host.appendChild(chip);
  });
}
function appendTextBlock(host, text) { if (!text || !text.trim()) return; var block = document.createElement('div'); block.className = 'chat-text-block'; block.textContent = text; host.appendChild(block); }
function splitMarkdownRow(line) { var cells = line.split('|').map(function(cell) { return cell.trim(); }); if (cells.length && cells[0] === '') cells.shift(); if (cells.length && cells[cells.length - 1] === '') cells.pop(); return cells; }
function isMarkdownSeparatorRow(line) { var cells = splitMarkdownRow(line); return !!cells.length && cells.every(function(cell) { return /^:?-{3,}:?$/.test(cell); }); }
function isTableStart(lines, index) { return index + 1 < lines.length && lines[index].indexOf('|') !== -1 && isMarkdownSeparatorRow(lines[index + 1]); }
function appendMarkdownTable(host, lines, startIndex) {
  var headers = splitMarkdownRow(lines[startIndex]);
  var tableWrap = document.createElement('div'); tableWrap.className = 'chat-markdown-table-wrap';
  var table = document.createElement('table'); table.className = 'chat-markdown-table';
  var thead = document.createElement('thead'); var headerRow = document.createElement('tr');
  headers.forEach(function(headerText) { var th = document.createElement('th'); th.textContent = headerText || 'Column'; headerRow.appendChild(th); });
  thead.appendChild(headerRow); table.appendChild(thead);
  var tbody = document.createElement('tbody'); var cursor = startIndex + 2;
  while (cursor < lines.length && lines[cursor].indexOf('|') !== -1 && lines[cursor].trim() !== '') {
    var rowValues = splitMarkdownRow(lines[cursor]); var tr = document.createElement('tr');
    for (var i = 0; i < headers.length; i++) { var td = document.createElement('td'); td.textContent = rowValues[i] || ''; tr.appendChild(td); }
    tbody.appendChild(tr); cursor += 1;
  }
  table.appendChild(tbody); tableWrap.appendChild(table); host.appendChild(tableWrap); return cursor;
}
function renderMessageBody(host, text) {
  var raw = text || ''; var lines = raw.split('\n'); var index = 0; var pending = [];
  while (index < lines.length) { if (isTableStart(lines, index)) { appendTextBlock(host, pending.join('\n')); pending = []; index = appendMarkdownTable(host, lines, index); continue; } pending.push(lines[index]); index += 1; }
  appendTextBlock(host, pending.join('\n'));
}
function buildChatMeta(meta) {
  if (!meta) return [];
  var pills = [];
  if (meta.source) pills.push('Source: ' + meta.source);
  if (meta.work_item_id) pills.push('Work Item: ' + meta.work_item_id);
  if (meta.selection_mode) pills.push('Selection: ' + meta.selection_mode);
  if (meta.presidio_check) pills.push('Presidio: ' + meta.presidio_check);
  if (meta.sanitization_mode) pills.push('Mode: ' + meta.sanitization_mode);
  if (typeof meta.uploaded_context_used === 'boolean') pills.push('Upload Context: ' + (meta.uploaded_context_used ? 'Yes' : 'No'));
  if (Array.isArray(meta.uploaded_sources) && meta.uploaded_sources.length) pills.push('Sources: ' + meta.uploaded_sources.join(', '));
  return pills;
}
function appendMessageToFeed(role, text, meta) {
  var feed = document.getElementById('chatFeed'); var row = document.createElement('div'); row.className = 'bubble';
  var avatar = document.createElement('div'); avatar.className = 'avatar ' + role; avatar.textContent = role === 'user' ? 'You' : 'AI';
  var card = document.createElement('div'); card.className = 'bubble-card';
  if (role === 'assistant') { var metaPills = buildChatMeta(meta); if (metaPills.length) { var metaRow = document.createElement('div'); metaRow.className = 'chat-meta'; metaPills.forEach(function(pillText) { var pill = document.createElement('span'); pill.className = 'chat-meta-pill'; pill.textContent = pillText; metaRow.appendChild(pill); }); card.appendChild(metaRow); } }
  var body = document.createElement('div'); renderMessageBody(body, text); card.appendChild(body);
  row.appendChild(avatar); row.appendChild(card); feed.appendChild(row); feed.scrollTop = feed.scrollHeight;
}
function renderChatFeed() {
  var feed = document.getElementById('chatFeed'); feed.innerHTML = ''; var session = getCurrentSession();
  if (!session) { appendMessageToFeed('assistant', 'Select a chat from History or click + New Chat to begin.'); renderAttachedFiles(); return; }
  if (!session.messages.length) { appendMessageToFeed('assistant', 'New chat started. Share your QA request and I will help with test design, coverage gaps, and automation guidance.'); renderAttachedFiles(); return; }
  session.messages.forEach(function(msg) { appendMessageToFeed(msg.role, msg.text, msg.meta || null); }); renderAttachedFiles();
}
function upsertCurrentMessage(role, text, meta) {
  var session = getCurrentSession();
  if (!session) { session = createChatSession(newSessionTitle()); chatSessions.push(session); currentChatId = session.id; }
  session.messages.push({ role: role, text: text, meta: meta || null, at: new Date().toISOString() });
  if (role === 'user') { var nonDefaultTitle = session.title && session.title !== newSessionTitle(); if (!nonDefaultTitle || session.messages.filter(function(msg) { return msg.role === 'user'; }).length === 1) session.title = truncateSessionTitle(text); }
  session.updatedAt = new Date().toISOString(); saveChatSessions(); renderChatHistory();
}
function addChatMessage(role, text, meta) { upsertCurrentMessage(role, text, meta || null); appendMessageToFeed(role, text, meta || null); }
function selectChatSession(sessionId) { currentChatId = sessionId; renderChatHistory(); renderChatFeed(); showView('freetext'); }
function startNewChatSession() { var current = getCurrentSession(); if (current && (!current.messages || current.messages.length === 0)) { showView('freetext'); return; } var session = createChatSession(newSessionTitle()); chatSessions.push(session); currentChatId = session.id; saveChatSessions(); renderChatHistory(); renderChatFeed(); showView('freetext'); }
async function removeAttachedDocument(documentId) {
  if (!documentId) return; var state = getCurrentContextState(); if (!state) return; if (!confirm('Remove this file from chat context?')) return;
  try {
    if (state.sessionId) {
      var res = await fetch('/api/context/' + encodeURIComponent(state.sessionId) + '/' + encodeURIComponent(documentId), { method: 'DELETE' });
      if (!res.ok) { var data = await res.json(); throw new Error(data && data.detail ? data.detail : 'Delete failed'); }
    }
    state.uploadedDocuments = (state.uploadedDocuments || []).filter(function(doc) { return String(doc.document_id || '') !== documentId; }); saveChatContextState(); renderAttachedFiles();
  } catch (err) {
    addChatMessage('assistant', 'Could not remove attached file: ' + String(err), { source: 'context_upload_error', grounded: false, presidio_check: 'unknown', sanitization_mode: 'unknown' });
  }
}
async function clearAllAttachedFiles() {
  var state = getCurrentContextState(); if (!state || !state.uploadedDocuments || !state.uploadedDocuments.length) return; if (!confirm('Remove all attached files from this chat context?')) return;
  try {
    if (state.sessionId) {
      var res = await fetch('/api/context/' + encodeURIComponent(state.sessionId), { method: 'DELETE' });
      if (!res.ok) { var data = await res.json(); throw new Error(data && data.detail ? data.detail : 'Clear failed'); }
    }
    state.uploadedDocuments = []; state.sessionId = null; saveChatContextState(); renderAttachedFiles();
  } catch (err) {
    addChatMessage('assistant', 'Could not clear attachments: ' + String(err), { source: 'context_upload_error', grounded: false, presidio_check: 'unknown', sanitization_mode: 'unknown' });
  }
}
function triggerContextUpload() { var picker = document.getElementById('contextFileInput'); if (picker) picker.click(); }
async function handleContextFilesSelected(event) {
  var picker = event && event.target ? event.target : document.getElementById('contextFileInput');
  if (!picker || !picker.files || !picker.files.length) return;
  var session = getCurrentSession(); if (!session) { startNewChatSession(); session = getCurrentSession(); }
  var state = getCurrentContextState(); if (!state) { picker.value = ''; return; }
  var attachBtn = document.getElementById('attachBtn'); if (attachBtn) { attachBtn.disabled = true; attachBtn.textContent = '...'; }
  var formData = new FormData(); Array.from(picker.files).forEach(function(file) { formData.append('files', file); }); if (state.sessionId) formData.append('session_id', state.sessionId);
  try {
    var res = await fetch('/api/context/upload', { method: 'POST', body: formData }); var data = await res.json(); if (!res.ok) throw new Error(data && data.detail ? data.detail : 'Upload failed');
    state.sessionId = data.session_id || state.sessionId; var uploaded = Array.isArray(data.uploaded_documents) ? data.uploaded_documents : []; uploaded.forEach(function(doc) { state.uploadedDocuments.push(doc); }); saveChatContextState(); renderAttachedFiles();
    addChatMessage('assistant', 'Uploaded ' + uploaded.length + ' file(s) and attached them to this chat context.', { source: 'context_upload', grounded: false, presidio_check: 'n/a', sanitization_mode: 'n/a' });
  } catch (err) {
    addChatMessage('assistant', 'File upload failed: ' + String(err), { source: 'context_upload_error', grounded: false, presidio_check: 'unknown', sanitization_mode: 'unknown' });
  } finally {
    if (attachBtn) { attachBtn.disabled = false; attachBtn.textContent = '+'; }
    picker.value = '';
  }
}
async function sendChat() {
  var box = document.getElementById('chatInput'); var text = box.value.trim(); if (!text) return;
  addChatMessage('user', text); box.value = '';
  var sendBtn = document.getElementById('sendBtn'); sendBtn.disabled = true; sendBtn.textContent = 'Sending...';
  var contextState = getCurrentContextState();
  try {
    var res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text, session_id: contextState && contextState.sessionId ? contextState.sessionId : null, include_uploaded_context: true }) });
    var data = await res.json(); addChatMessage('assistant', data.assistant_message || 'No response available.', data.response_meta || null);
  } catch (err) {
    addChatMessage('assistant', 'Request failed: ' + String(err), { source: 'error', presidio_check: 'unknown', sanitization_mode: 'unknown' });
  } finally { sendBtn.disabled = false; sendBtn.textContent = 'Send'; }
}

document.getElementById('chatInput').addEventListener('keydown', function(event) {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendChat(); }
});
document.getElementById('pfTopLevels').addEventListener('input', function() { applyPredictiveTopLevels(); });
document.getElementById('pfModuleFilter').addEventListener('change', function() { repopulatePredictiveFunctionDropdown(''); applyPredictiveTopLevels(); });
document.getElementById('pfFunctionFilter').addEventListener('change', function() { applyPredictiveTopLevels(); });
showView('home');
chatSessions = loadChatSessions();
chatContextBySession = loadChatContextState();
currentChatId = null;
renderChatHistory();
renderChatFeed();
