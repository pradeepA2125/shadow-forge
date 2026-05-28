(function () {
  const vscode = acquireVsCodeApi();
  const threadEl = document.getElementById('thread');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  let currentAgentBubble = null;
  let thinkingEl = null;
  let activeThinkingLi = null;

  // Signal the extension that the webview is ready to receive messages.
  vscode.postMessage({ type: 'webviewReady' });

  document.getElementById('new-chat-btn').addEventListener('click', () => {
    vscode.postMessage({ type: 'newChat' });
  });

  function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    vscode.postMessage({ type: 'sendMessage', text });
  }
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function showThinking(message) {
    if (thinkingEl) { thinkingEl.querySelector('span').textContent = message; return; }
    thinkingEl = document.createElement('div');
    thinkingEl.className = 'thinking';
    thinkingEl.innerHTML = '<div class="thinking-dot"></div><span>' + escHtml(message) + '</span>';
    threadEl.appendChild(thinkingEl);
    threadEl.scrollTop = threadEl.scrollHeight;
  }
  function updateThinking(message) {
    if (thinkingEl) thinkingEl.querySelector('span').textContent = message;
  }
  function hideThinking() {
    if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  }

  function renderThreadList(threads, activeId) {
    var list = document.getElementById('thread-list');
    list.querySelectorAll('.thread-tab').forEach(function (el) { el.remove(); });
    var btn = document.getElementById('new-chat-btn');
    threads.forEach(function (t) {
      var tab = document.createElement('button');
      tab.className = 'thread-tab' + (t.threadId === activeId ? ' active' : '');
      tab.setAttribute('data-thread-id', t.threadId);
      tab.textContent = t.title || 'New Chat';
      tab.onclick = function () { vscode.postMessage({ type: 'switchThread', threadId: t.threadId }); };
      list.insertBefore(tab, btn);
    });
  }

  function buildThinkingLog(log) {
    if (!log || !log.length) return '';
    var items = log.map(function (entry) {
      return '<li>' + escHtml(entry) + '</li>';
    }).join('');
    return '<details class="thinking-log"><summary>Show thinking (' + log.length + ' steps)</summary><ul>' + items + '</ul></details>';
  }

  function collapseOpenThinkingPane(bubble) {
    if (!bubble) return;
    var details = bubble.querySelector('.thinking-log');
    if (details && details.hasAttribute('open')) {
      details.removeAttribute('open');
      var count = details.querySelectorAll('li').length;
      details.querySelector('summary').textContent = 'Show thinking (' + count + ' steps)';
    }
  }

  function appendMessage(msg) {
    collapseOpenThinkingPane(currentAgentBubble);
    currentAgentBubble = null;
    activeThinkingLi = null;
    if (msg.type === 'plan_card') {
      var taskId = escHtml((msg.metadata && msg.metadata.taskId) ? msg.metadata.taskId : (msg.taskId || ''));
      var mdHtml = (typeof marked !== 'undefined' && msg.content)
        ? marked.parse(msg.content)
        : '<pre>' + escHtml(msg.content) + '</pre>';
      var div = document.createElement('div');
      div.className = 'plan-card';
      div.innerHTML =
        '<strong>Plan</strong><div class="plan-md">' + mdHtml + '</div>' +
        '<div class="plan-actions">' +
        '<button class="btn-primary" data-taskid="' + taskId + '" data-action="implement">Implement Plan</button>' +
        '<textarea id="fb-' + taskId + '" placeholder="Give feedback…" rows="2"></textarea>' +
        '<button class="btn-secondary" data-taskid="' + taskId + '" data-action="feedback">Send Feedback</button>' +
        '</div>';
      div.querySelectorAll('button[data-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var tid = btn.getAttribute('data-taskid');
          var action = btn.getAttribute('data-action');
          var actionsEl = div.querySelector('.plan-actions');
          if (action === 'implement') {
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">Implementing…</span>';
            vscode.postMessage({ type: 'implementPlan', taskId: tid });
          } else {
            var fbEl = document.getElementById('fb-' + tid);
            var fb = fbEl ? fbEl.value.trim() : '';
            if (!fb) return;
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">Feedback submitted — new plan loading…</span>';
            vscode.postMessage({ type: 'planFeedback', taskId: tid, feedback: fb });
          }
        });
      });
      threadEl.appendChild(div);
    } else if (msg.type === 'scope_card') {
      var taskId = escHtml((msg.metadata && msg.metadata.taskId) ? msg.metadata.taskId : '');
      var files = (msg.metadata && Array.isArray(msg.metadata.files)) ? msg.metadata.files : [];
      var reason = (msg.metadata && msg.metadata.reason) ? msg.metadata.reason : '';
      var stepId = (msg.metadata && msg.metadata.step_id) ? msg.metadata.step_id : '';
      var filesJson = escHtml(JSON.stringify(files));
      var fileLines = files.map(function (f) {
        return '<span class="diff-file">' + escHtml(f) + '</span>';
      }).join('<br>');
      var div = document.createElement('div');
      div.className = 'plan-card';
      div.setAttribute('data-scope-task-id', taskId);
      div.innerHTML =
        '<strong>Scope extension requested</strong>' +
        (stepId ? ' <span style="opacity:0.6;font-size:0.85em">[step ' + escHtml(stepId) + ']</span>' : '') +
        (reason ? '<div style="margin:4px 0 6px;opacity:0.85">' + escHtml(reason) + '</div>' : '') +
        (fileLines ? '<div class="diff-files">' + fileLines + '</div>' : '') +
        '<div class="plan-actions">' +
        '<button class="btn-primary" data-taskid="' + taskId + '" data-files="' + filesJson + '" data-action="approve">Approve</button>' +
        '<button class="btn-primary" data-taskid="' + taskId + '" data-files="' + filesJson + '" data-action="approve-remember">Approve & Remember</button>' +
        '<button class="btn-secondary" data-taskid="' + taskId + '" data-files="' + filesJson + '" data-action="reject">Reject</button>' +
        '</div>';
      div.querySelectorAll('button[data-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var tid = btn.getAttribute('data-taskid');
          var action = btn.getAttribute('data-action');
          var rawFiles = btn.getAttribute('data-files');
          var parsedFiles = [];
          try { parsedFiles = JSON.parse(rawFiles || '[]'); } catch (_) {}
          var actionsEl = div.querySelector('.plan-actions');
          if (action === 'approve' || action === 'approve-remember') {
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✓ Approved</span>';
            vscode.postMessage({ type: 'scopeDecision', taskId: tid, files: parsedFiles, decision: 'approve', remember: action === 'approve-remember' });
          } else {
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✗ Rejected</span>';
            vscode.postMessage({ type: 'scopeDecision', taskId: tid, files: parsedFiles, decision: 'reject', remember: false });
          }
        });
      });
      threadEl.appendChild(div);
    } else if (msg.type === 'validation_card') {
      var taskId = escHtml((msg.metadata && msg.metadata.taskId) ? msg.metadata.taskId : '');
      var diags = (msg.metadata && Array.isArray(msg.metadata.diagnostics)) ? msg.metadata.diagnostics : [];
      var diagLines = diags.map(function (d) {
        var level = escHtml(d.level || 'error');
        var text = escHtml(String(d.message || '').slice(0, 600));
        return '<div class="diff-file">[' + level + '] ' + text + '</div>';
      }).join('');
      var div = document.createElement('div');
      div.className = 'plan-card';
      div.setAttribute('data-validation-task-id', taskId);
      div.innerHTML =
        '<strong>Validation failed — review</strong>' +
        '<div style="margin:4px 0 6px;opacity:0.85">These errors remained after auto-repair. They may be pre-existing or unrelated — accept to proceed to review, or reject to fail.</div>' +
        (diagLines ? '<div class="diff-files">' + diagLines + '</div>' : '') +
        '<div class="plan-actions">' +
        '<button class="btn-primary" data-taskid="' + taskId + '" data-action="accept">Accept</button>' +
        '<button class="btn-secondary" data-taskid="' + taskId + '" data-action="reject">Reject</button>' +
        '</div>';
      div.querySelectorAll('button[data-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var tid = btn.getAttribute('data-taskid');
          var action = btn.getAttribute('data-action');
          var actionsEl = div.querySelector('.plan-actions');
          if (action === 'accept') {
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✓ Accepted</span>';
            vscode.postMessage({ type: 'validationDecision', taskId: tid, decision: 'accept' });
          } else {
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✗ Rejected</span>';
            vscode.postMessage({ type: 'validationDecision', taskId: tid, decision: 'reject' });
          }
        });
      });
      threadEl.appendChild(div);
    } else if (msg.type === 'command_card') {
      var taskId = escHtml((msg.metadata && msg.metadata.taskId) ? msg.metadata.taskId : '');
      var command = (msg.metadata && msg.metadata.command) ? String(msg.metadata.command) : '';
      var args = (msg.metadata && Array.isArray(msg.metadata.args)) ? msg.metadata.args.map(String) : [];
      var tokens = [command].concat(args).filter(function (t) { return t.length > 0; });
      // shlex.join-equivalent: single-quote tokens containing whitespace or shell metacharacters.
      function shlexJoin(toks) {
        return toks.map(function (t) {
          if (/[ \t\n"'\\$|&;<>()*?\[\]{}#~`]/.test(t)) {
            return "'" + t.replace(/'/g, "'\"'\"'") + "'";
          }
          return t;
        }).join(' ');
      }
      var binary = command.split('/').pop() || command;
      var chipsHtml = tokens.map(function (t, i) {
        return '<span class="diff-file" data-tok-i="' + i + '" style="padding:2px 6px;margin:2px;border:1px solid var(--vscode-panel-border);border-radius:3px;display:inline-block">' + escHtml(t) + '</span>';
      }).join('');
      var div = document.createElement('div');
      div.className = 'plan-card';
      div.setAttribute('data-command-task-id', taskId);
      div.innerHTML =
        '<strong>Run command?</strong>' +
        '<div style="margin:4px 0 6px;font-family:monospace;font-size:0.95em">' + chipsHtml + '</div>' +
        '<div style="margin:6px 0">' +
          '<label style="margin-right:10px"><input type="radio" name="scope-' + taskId + '" value="exact" checked> Exact (this command only)</label>' +
          '<label style="margin-right:10px"><input type="radio" name="scope-' + taskId + '" value="prefix"> Prefix — lock first <input type="number" class="prefix-cut" min="1" max="' + tokens.length + '" value="1" style="width:42px"> token(s)</label>' +
          '<label><input type="radio" name="scope-' + taskId + '" value="binary"> Any <code>' + escHtml(binary) + '</code></label>' +
        '</div>' +
        '<div class="auto-approves-preview" style="margin:4px 0 8px;opacity:0.8;font-size:0.85em"></div>' +
        '<div class="plan-actions">' +
          '<button class="btn-secondary" data-action="reject">Reject</button>' +
          '<button class="btn-secondary" data-action="accept-once">Accept once</button>' +
          '<button class="btn-primary" data-action="accept-remember">Accept &amp; remember</button>' +
        '</div>';

      var previewEl = div.querySelector('.auto-approves-preview');
      var cutInput = div.querySelector('.prefix-cut');
      function selectedScope() {
        var checked = div.querySelector('input[name="scope-' + taskId + '"]:checked');
        return checked ? checked.value : 'exact';
      }
      function ruleValueForScope(scope) {
        if (scope === 'binary') return binary;
        if (scope === 'exact') return shlexJoin(tokens);
        // prefix
        var n = Math.max(1, Math.min(tokens.length, parseInt(cutInput.value, 10) || 1));
        return shlexJoin(tokens.slice(0, n));
      }
      function updatePreview() {
        var scope = selectedScope();
        if (scope === 'exact') {
          previewEl.textContent = 'auto-approves: this exact command';
        } else if (scope === 'binary') {
          previewEl.textContent = 'auto-approves: any "' + binary + ' …"';
        } else {
          previewEl.textContent = 'auto-approves: ' + ruleValueForScope('prefix') + ' …';
        }
      }
      div.querySelectorAll('input[name="scope-' + taskId + '"]').forEach(function (r) {
        r.addEventListener('change', updatePreview);
      });
      cutInput.addEventListener('input', updatePreview);
      updatePreview();

      div.querySelectorAll('button[data-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var action = btn.getAttribute('data-action');
          var actionsEl = div.querySelector('.plan-actions');
          var scope = selectedScope();
          var payload = { type: 'commandDecision', taskId: taskId };
          if (action === 'reject') {
            payload.approve = false;
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✗ Rejected</span>';
          } else if (action === 'accept-once') {
            payload.approve = true;
            payload.remember = false;
            payload.scope = 'exact';
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✓ Accepted once</span>';
          } else {
            // accept-remember
            payload.approve = true;
            payload.remember = true;
            payload.scope = scope;
            payload.ruleValue = ruleValueForScope(scope);
            if (actionsEl) actionsEl.innerHTML = '<span class="inline-resolved">✓ Accepted &amp; remembered</span>';
          }
          vscode.postMessage(payload);
        });
      });
      threadEl.appendChild(div);
    } else if (msg.type === 'task_card') {
      var taskId = msg.taskId || msg.content || '';
      var div = document.createElement('div');
      div.className = 'plan-card';
      div.innerHTML =
        '<strong>Task created</strong>' +
        '<div class="diff-files" style="margin-top:6px">Task <span style="font-family:monospace">' + escHtml(taskId) + '</span> is queued — track it in the Tasks panel.</div>';
      threadEl.appendChild(div);
    } else if (msg.type === 'diff_card') {
      var taskId = msg.taskId || (msg.metadata && msg.metadata.taskId) || '';
      var entries = (msg.metadata && Array.isArray(msg.metadata.diff_entries)) ? msg.metadata.diff_entries : [];
      var resolved = msg.metadata && msg.metadata.resolved;
      var fileLines = entries.map(function (e) {
        return '<span class="diff-file">' + escHtml(e.path) + '</span>' +
          ' <span class="diff-adds">+' + (e.additions || 0) + '</span>' +
          ' <span class="diff-dels">-' + (e.deletions || 0) + '</span>';
      }).join('<br>');
      var div = document.createElement('div');
      div.className = 'plan-card';
      div.setAttribute('data-inline-task-id', taskId);
      var actionsHtml;
      if (resolved) {
        actionsHtml = '<div class="plan-actions"><span class="inline-resolved">' +
          (resolved === 'applied' ? '✓ Applied' : '✗ Discarded') + '</span></div>';
      } else {
        var viewButtons = entries.map(function (e) {
          return '<button class="btn-ghost" data-action="viewdiff" data-path="' + escHtml(e.path) + '" data-shadow-path="' + escHtml(e.temp_path || '') + '">⎘ ' + escHtml(e.path.split('/').pop()) + '</button>';
        }).join('');
        actionsHtml =
          '<div class="plan-actions">' +
          '<button class="btn-primary" data-taskid="' + escHtml(taskId) + '" data-action="apply">Apply</button>' +
          '<button class="btn-secondary" data-taskid="' + escHtml(taskId) + '" data-action="discard">Discard</button>' +
          (viewButtons ? '<div class="diff-view-btns">' + viewButtons + '</div>' : '') +
          '</div>';
      }
      var thinkingHtml = buildThinkingLog(msg.metadata && msg.metadata.thinking_log);
      div.innerHTML =
        '<strong>Changes ready</strong>' +
        (fileLines ? '<div class="diff-files">' + fileLines + '</div>' : '') +
        thinkingHtml +
        actionsHtml;
      if (!resolved) {
        div.querySelectorAll('button[data-action]').forEach(function (btn) {
          btn.addEventListener('click', function () {
            var tid = btn.getAttribute('data-taskid');
            var action = btn.getAttribute('data-action');
            if (action === 'apply') {
              vscode.postMessage({ type: 'applyInlineChange', taskId: tid });
            } else if (action === 'discard') {
              vscode.postMessage({ type: 'discardInlineChange', taskId: tid });
            } else if (action === 'viewdiff') {
              vscode.postMessage({ type: 'viewDiffFile', path: btn.getAttribute('data-path'), shadowPath: btn.getAttribute('data-shadow-path') || '' });
            }
          });
        });
      }
      threadEl.appendChild(div);
    } else {
      var div = document.createElement('div');
      div.className = 'msg ' + (msg.role === 'user' ? 'user' : 'agent');
      var thinkingHtml = msg.role === 'agent' ? buildThinkingLog(msg.metadata && msg.metadata.thinking_log) : '';
      if (thinkingHtml) {
        var span = document.createElement('span');
        span.className = 'agent-text';
        span.textContent = msg.content;
        div.appendChild(span);
        div.insertAdjacentHTML('beforeend', thinkingHtml);
      } else {
        div.textContent = msg.content;
      }
      threadEl.appendChild(div);
    }
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  function appendChunk(chunk) {
    if (!currentAgentBubble) {
      currentAgentBubble = document.createElement('div');
      currentAgentBubble.className = 'msg agent';
      threadEl.appendChild(currentAgentBubble);
    }
    // Collapse the live thinking pane when text starts arriving.
    var details = currentAgentBubble.querySelector('.thinking-log');
    if (details && details.hasAttribute('open')) {
      details.removeAttribute('open');
      var count = details.querySelectorAll('li').length;
      details.querySelector('summary').textContent = 'Show thinking (' + count + ' steps)';
    }
    var textEl = currentAgentBubble.querySelector('.agent-text');
    if (!textEl) {
      textEl = document.createElement('span');
      textEl.className = 'agent-text';
      currentAgentBubble.appendChild(textEl);
    }
    textEl.textContent += chunk;
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  function ensureThinkingPane() {
    if (!currentAgentBubble) {
      currentAgentBubble = document.createElement('div');
      currentAgentBubble.className = 'msg agent';
      threadEl.appendChild(currentAgentBubble);
    }
    var details = currentAgentBubble.querySelector('.thinking-log');
    if (!details) {
      details = document.createElement('details');
      details.className = 'thinking-log';
      details.setAttribute('open', '');
      details.innerHTML = '<summary>Thinking…</summary><ul></ul>';
      currentAgentBubble.appendChild(details);
    }
    return details;
  }

  function appendThinkingChunk(chunk) {
    var details = ensureThinkingPane();
    if (!activeThinkingLi) {
      activeThinkingLi = document.createElement('li');
      details.querySelector('ul').appendChild(activeThinkingLi);
    }
    activeThinkingLi.textContent += chunk;
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  function appendThinkingEntry(text) {
    // Seal any in-progress streaming <li> before adding a new discrete entry.
    activeThinkingLi = null;
    var details = ensureThinkingPane();
    var ul = details.querySelector('ul');
    var li = document.createElement('li');
    li.textContent = text;
    ul.appendChild(li);
    var count = ul.querySelectorAll('li').length;
    details.querySelector('summary').textContent = 'Thinking (' + count + ')…';
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  window.addEventListener('message', function (e) {
    var msg = e.data;
    if (msg.type === 'appendMessage') { hideThinking(); appendMessage(msg.message); }
    else if (msg.type === 'appendThinkingEntry') { appendThinkingEntry(msg.text); }
    else if (msg.type === 'appendThinkingChunk') { appendThinkingChunk(msg.chunk); }
    else if (msg.type === 'appendChunk') { hideThinking(); appendChunk(msg.chunk); }
    else if (msg.type === 'showThinking') showThinking(msg.message);
    else if (msg.type === 'updateThinking') updateThinking(msg.message);
    else if (msg.type === 'hideThinking') hideThinking();
    else if (msg.type === 'setInputEnabled') {
      input.disabled = !msg.enabled;
      sendBtn.disabled = !msg.enabled;
    } else if (msg.type === 'renderThreadList') {
      renderThreadList(msg.threads, msg.activeThreadId);
    } else if (msg.type === 'clearThread') {
      hideThinking();
      threadEl.innerHTML = '';
      currentAgentBubble = null;
    } else if (msg.type === 'thread_title_updated') {
      var tab = threadEl.closest('body')
        ? document.querySelector('.thread-tab[data-thread-id="' + msg.payload.thread_id + '"]')
        : null;
      if (tab) { tab.textContent = msg.payload.title; }
    } else if (msg.type === 'finalizeAgentMessage') {
      collapseOpenThinkingPane(currentAgentBubble);
      currentAgentBubble = null;
    } else if (msg.type === 'resolveInlineChangeCard') {
      var card = threadEl.querySelector('[data-inline-task-id="' + msg.taskId + '"]');
      if (card) {
        var actions = card.querySelector('.plan-actions');
        if (actions) {
          actions.innerHTML = '<span class="inline-resolved">' +
            (msg.resolution === 'applied' ? '✓ Applied' : '✗ Discarded') + '</span>';
        }
      }
    }
  });
}());
