// Common two-letter language codes, used only to *suggest* a value for the
// language filter from whatever URL is pasted in — not exhaustive (the
// backend's ISO 639-1 set is authoritative for actually filtering), just
// needs to avoid suggesting something wrong.
const COMMON_LANGUAGE_CODES = new Set([
  "en", "es", "fr", "de", "it", "pt", "nl", "ru", "ja", "zh", "ko", "ar", "hi", "tr", "pl",
  "sv", "da", "no", "fi", "cs", "el", "he", "th", "vi", "id", "ms", "ro", "hu", "uk", "bg",
  "sk", "sl", "hr", "sr", "lt", "lv", "et", "ca", "eu", "gl", "is", "ga", "cy", "sq", "mk",
  "az", "ka", "hy", "kk", "uz", "mn", "fa", "ur", "bn", "ta", "te", "ml", "kn", "mr", "gu",
  "pa", "si", "km", "lo", "my", "ne", "am",
]);

function suggestLanguageFromUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    if (!segments.length) return "";
    const code = segments[0].toLowerCase().split("-")[0].split("_")[0];
    return code.length === 2 && COMMON_LANGUAGE_CODES.has(code) ? code : "";
  } catch (e) {
    return "";
  }
}

function initHomeForm() {
  const form = document.getElementById("crawl-form");
  const errorEl = document.getElementById("form-error");
  const submitBtn = document.getElementById("submit-btn");
  const urlInput = document.getElementById("url");
  const domainScopeSelect = document.getElementById("domain_scope");
  const languageInput = document.getElementById("language");
  const exclusionsInput = document.getElementById("exclusions");

  urlInput.addEventListener("blur", () => {
    if (!languageInput.value) {
      const suggestion = suggestLanguageFromUrl(urlInput.value);
      if (suggestion) languageInput.value = suggestion;
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.style.display = "none";
    submitBtn.disabled = true;
    submitBtn.textContent = "Starting…";

    const url = urlInput.value;
    const domainScope = domainScopeSelect.value;
    const language = languageInput.value.trim() || null;
    const exclusions = exclusionsInput.value.trim() || null;

    try {
      const res = await fetch("/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url, domain_scope: domainScope, language, exclusions,
          capture_markdown: document.getElementById("capture-markdown").checked,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Something went wrong");
      }
      window.location.href = "/crawl/" + data.run_id;
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = "Crawl";
    }
  });
}

function badgeClass(status) {
  if (status === "completed") return "status-badge status-completed";
  if (status === "failed") return "status-badge status-failed";
  if (status === "cancelled") return "status-badge status-cancelled";
  if (status === "paused") return "status-badge status-paused";
  if (status === "queued") return "status-badge status-queued";
  return "status-badge status-crawling";
}

function statusLabel(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "paused"];
// A summary email goes out on each of these (see send_crawl_notification in
// crawler.py). "paused" is deliberately absent — that run still has to resume
// and finish before anything is sent.
const EMAILED_STATUSES = ["completed", "failed", "cancelled"];

const DOMAIN_SCOPE_LABELS = {
  all: "Whole domain (incl. subdomains)",
  subdomain_only: "This subdomain only",
  top_domain_only: "Top-level domain only",
};

function parseLanguageList(text) {
  if (!text) return [];
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

function renderSettingsPills(domainScope, languages, autoDetected, exclusions) {
  const domainPill = document.getElementById("domain-scope-pill");
  const languagePill = document.getElementById("language-pill");
  const exclusionsPill = document.getElementById("exclusions-pill");
  if (domainPill) domainPill.textContent = DOMAIN_SCOPE_LABELS[domainScope] || domainScope;
  if (exclusionsPill) {
    // parseLanguageList is just a comma splitter, reused here.
    const entries = parseLanguageList(exclusions);
    exclusionsPill.style.display = entries.length ? "" : "none";
    // textContent, not innerHTML: unlike the scope and language pills this is
    // free text somebody typed, and a shared report renders whatever its owner
    // entered to everyone they shared it with.
    exclusionsPill.textContent = entries.length ? `Excluded: ${entries.join(", ")}` : "";
  }
  if (!languagePill) return;
  if (!languages.length) {
    languagePill.textContent = "Languages: All";
  } else if (languages.length === 1) {
    languagePill.textContent = autoDetected ? `Language: ${languages[0]} (auto)` : `Language: ${languages[0]}`;
    languagePill.innerHTML = autoDetected
      ? `Language: <strong>${languages[0]}</strong> (auto)`
      : `Language: <strong>${languages[0]}</strong>`;
  } else {
    const [primary, ...rest] = languages;
    languagePill.innerHTML = `Languages: <strong>${primary}</strong> + ${rest.join(", ")}`;
  }
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const datePart = d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  const timePart = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  return `${datePart} · ${timePart}`;
}

// Run timestamps are stored as UTC and rendered server-side as a raw ISO
// string, so the browser is what turns them into the reader's local time.
function formatRunDates() {
  for (const el of document.querySelectorAll(".run-date-iso")) {
    el.textContent = formatDate(el.dataset.iso);
  }
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const mb = bytes / 1048576;
  if (mb < 1) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

function formatDuration(totalSeconds) {
  if (!totalSeconds || totalSeconds < 1) return "less than a minute";
  const totalMinutes = Math.round(totalSeconds / 60);
  if (totalMinutes < 1) return "less than a minute";
  if (totalMinutes < 60) return `~${totalMinutes} min`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes ? `~${hours}h ${minutes}m` : `~${hours}h`;
}

function concurrencyLabel(count) {
  if (count <= 1) return "Easy";
  if (count === 2) return "Moderate";
  return "Busy";
}

function renderPageRow(tbody, page, append) {
  const tr = document.createElement("tr");
  if (page.blocked_by_host) {
    tr.className = "page-blocked-host";
    tr.title = page.error || "Blocked by the host's bot detection";
  } else if (!page.success) {
    tr.className = "page-failed";
    tr.title = page.error || "Failed to fetch page";
  }
  const urlTd = document.createElement("td");
  urlTd.className = "url-cell";
  urlTd.title = page.url;
  urlTd.textContent = page.url;
  const titleTd = document.createElement("td");
  titleTd.textContent = page.title || "";
  const wordsTd = document.createElement("td");
  wordsTd.className = "words-cell";
  wordsTd.textContent = page.blocked_by_host ? "blocked" : page.success ? page.word_count.toLocaleString("en-US") : "failed";
  const openTd = document.createElement("td");
  openTd.className = "open-cell";
  const openLink = document.createElement("a");
  openLink.href = page.url;
  openLink.target = "_blank";
  openLink.rel = "noopener noreferrer";
  openLink.className = "open-link";
  openLink.textContent = "Open ↗";
  openTd.appendChild(openLink);
  tr.append(urlTd, titleTd, wordsTd, openTd);
  // Live mode prepends so the newest page appears at the top as it streams in;
  // a saved report appends so paged-in rows continue the list in order.
  if (append) tbody.appendChild(tr);
  else tbody.prepend(tr);
}

// A saved report never puts more than this many rows in the DOM. Past roughly
// this point the table stops being something anyone reads and starts being the
// reason the tab hangs — 164k rows is a million DOM nodes. The CSV export is
// the complete list.
const PAGE_LIST_MAX = 5000;

function folderForUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    return segments.length ? "/" + segments[0] : "(root)";
  } catch (e) {
    return "(root)";
  }
}

const SUMMARY_ROW_LIMIT = 10;

function renderExpandableTableRows(tbody, rows, colSpan, renderRowFn) {
  const fill = (items) => {
    tbody.innerHTML = "";
    for (const item of items) tbody.appendChild(renderRowFn(item));
  };
  fill(rows.slice(0, SUMMARY_ROW_LIMIT));
  if (rows.length <= SUMMARY_ROW_LIMIT) return;
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = colSpan;
  td.className = "show-all-row";
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = `Show all ${rows.length.toLocaleString("en-US")}`;
  link.addEventListener("click", (e) => {
    e.preventDefault();
    fill(rows);
  });
  td.appendChild(link);
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function renderFolderRow(group, maxWords) {
  // Live mode still groups in the browser and passes [name, {count, words}];
  // a saved report gets {name, pages, words} already grouped by the server.
  const name = Array.isArray(group) ? group[0] : group.name;
  const words = Array.isArray(group) ? group[1].words : group.words;
  const count = Array.isArray(group) ? group[1].count : group.pages;
  const tr = document.createElement("tr");

  const folderTd = document.createElement("td");
  const bar = document.createElement("div");
  bar.className = "folder-bar";
  bar.style.width = Math.max(1, Math.round((words / maxWords) * 100)) + "%";
  const folderSpan = document.createElement("span");
  folderSpan.textContent = name;
  folderTd.append(bar, folderSpan);

  const countTd = document.createElement("td");
  countTd.className = "folder-pages";
  countTd.innerHTML = `<span>${count.toLocaleString("en-US")}</span>`;

  const wordsTd = document.createElement("td");
  wordsTd.className = "folder-words";
  wordsTd.innerHTML = `<span>${words.toLocaleString("en-US")}</span>`;

  tr.append(folderTd, countTd, wordsTd);
  return tr;
}

function renderFolderGroups(pages) {
  const groups = {};
  for (const p of pages) {
    const folder = folderForUrl(p.url);
    if (!groups[folder]) groups[folder] = { words: 0, count: 0 };
    groups[folder].words += p.word_count || 0;
    groups[folder].count += 1;
  }
  const rows = Object.entries(groups).sort((a, b) => b[1].words - a[1].words);
  const maxWords = rows.length ? rows[0][1].words : 1;
  const tbody = document.getElementById("folder-tbody");
  renderExpandableTableRows(tbody, rows, 3, (row) => renderFolderRow(row, maxWords));
}

// Same ISO 639-1 set and first-path-segment heuristic as the backend's
// LanguageFilter (app/crawler.py) — kept in sync manually, purely for
// grouping the results table by language, not for filtering anything.
const ISO_639_1_CODES = new Set([
  "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
  "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
  "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
  "da", "de", "dv", "dz",
  "ee", "el", "en", "eo", "es", "et", "eu",
  "fa", "ff", "fi", "fj", "fo", "fr", "fy",
  "ga", "gd", "gl", "gn", "gu", "gv",
  "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
  "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
  "ja", "jv",
  "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
  "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
  "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
  "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
  "oc", "oj", "om", "or", "os",
  "pa", "pi", "pl", "ps", "pt",
  "qu",
  "rm", "rn", "ro", "ru", "rw",
  "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw",
  "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
  "ug", "uk", "ur", "uz",
  "ve", "vi", "vo",
  "wa", "wo",
  "xh",
  "yi", "yo",
  "za", "zh", "zu",
]);

function languageForUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    if (!segments.length) return "Default";
    const code = segments[0].toLowerCase().split("-")[0].split("_")[0];
    return code.length === 2 && ISO_639_1_CODES.has(code) ? code : "Default";
  } catch (e) {
    return "Default";
  }
}

function renderLanguageGroups(pages) {
  const groups = {};
  for (const p of pages) {
    const lang = languageForUrl(p.url);
    if (!groups[lang]) groups[lang] = { words: 0, count: 0 };
    groups[lang].words += p.word_count || 0;
    groups[lang].count += 1;
  }
  const section = document.getElementById("language-section");
  const rows = Object.entries(groups).sort((a, b) => b[1].words - a[1].words);
  // A single group (e.g. every crawl with no multi-language filter applied)
  // isn't worth a whole extra table — only show this when it's informative.
  if (rows.length < 2) {
    section.style.display = "none";
    return;
  }
  const maxWords = rows[0][1].words || 1;
  const tbody = document.getElementById("language-tbody");
  renderExpandableTableRows(tbody, rows, 3, (row) => renderFolderRow(row, maxWords));
  section.style.display = "";
}

function renderPageListItem(page, i) {
  const li = document.createElement("li");
  const rank = document.createElement("span");
  rank.className = "rank";
  rank.textContent = i + 1;
  const path = document.createElement("span");
  path.className = "path";
  path.title = page.url;
  path.textContent = page.url;
  const words = document.createElement("span");
  words.className = "words";
  words.textContent = page.word_count.toLocaleString("en-US");
  li.append(rank, path, words);
  return li;
}

// Shows the first SUMMARY_ROW_LIMIT rows with a link that reveals the rest.
// `label` overrides the link text when the list itself is already a cut down
// version of a much longer one.
function fillExpandableList(list, rows, total, label) {
  const fill = (items) => {
    list.innerHTML = "";
    for (const row of items) list.appendChild(row);
  };
  fill(rows.slice(0, SUMMARY_ROW_LIMIT));
  if (rows.length <= SUMMARY_ROW_LIMIT) return;

  const li = document.createElement("li");
  li.className = "show-all-row";
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = label || `Show all ${total.toLocaleString("en-US")}`;
  link.addEventListener("click", (e) => {
    e.preventDefault();
    fill(rows);
  });
  li.appendChild(link);
  list.appendChild(li);
}

function renderTopPages(pages) {
  const top = pages.filter((p) => p.success).sort((a, b) => b.word_count - a.word_count);
  fillExpandableList(
    document.getElementById("top-pages-list"),
    top.map((p, i) => renderPageListItem(p, i)),
    top.length,
    null,
  );
}

function pagesToCsv(pages) {
  const escapeCell = (value) => {
    const s = String(value);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const rows = [["URL", "Title", "Words", "Status", "Error"]];
  for (const p of pages) {
    const status = p.blocked_by_host ? "blocked" : p.login_required ? "login_required" : p.success ? "ok" : "failed";
    rows.push([p.url, p.title || "", p.success ? p.word_count : "", status, p.error || ""]);
  }
  return rows.map((row) => row.map(escapeCell).join(",")).join("\r\n");
}

function downloadCsv(filename, csvText) {
  const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function renderSummary(pages) {
  renderTopPages(pages);
  renderFolderGroups(pages);
  renderLanguageGroups(pages);
  document.getElementById("summary").style.display = "";
}

// Saved reports get their totals already grouped by the server, so the browser
// never has to hold every crawled page just to add up folders. See app/report.py.
function renderServerSummary(summary) {
  const topList = document.getElementById("top-pages-list");
  const topRows = summary.top_pages.map((p, i) => renderPageListItem(p, i));
  fillExpandableList(topList, topRows, summary.top_pages.length,
    summary.top_pages_capped ? `Show top ${summary.top_pages.length.toLocaleString("en-US")}` : null);

  const renderGroups = (rows, tbodyId) => {
    const max = rows.length ? rows[0].words : 1;
    renderExpandableTableRows(document.getElementById(tbodyId), rows, 3, (row) => renderFolderRow(row, max));
  };
  renderGroups(summary.folders, "folder-tbody");

  const section = document.getElementById("language-section");
  if (summary.languages.length < 2) {
    section.style.display = "none";
  } else {
    renderGroups(summary.languages, "language-tbody");
    section.style.display = "";
  }

  document.getElementById("summary").style.display = "";
}

function setStatCount(el, count) {
  el.textContent = count.toLocaleString("en-US");
  el.classList.toggle("zero", count === 0);
}

function initCrawlPage(opts) {
  const statusBadge = document.getElementById("status-badge");
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const totalWordsEl = document.getElementById("total-words");
  const pageCountEl = document.getElementById("page-count");
  const pagesHeadingCountEl = document.getElementById("pages-heading-count");
  const loginBlockedEl = document.getElementById("login-blocked-count");
  const duplicateEl = document.getElementById("duplicate-count");
  const limitNote = document.getElementById("limit-note");
  const cancelNote = document.getElementById("cancel-note");
  const pausedPastNote = document.getElementById("paused-past-note");
  const errorBox = document.getElementById("error-box");
  const tbody = document.getElementById("page-tbody");
  const cancelBtn = document.getElementById("cancel-btn");
  const actionRow = document.getElementById("action-row");
  const recrawlForm = document.getElementById("recrawl-form");
  const recrawlBtn = document.getElementById("recrawl-btn");
  const recrawlDomainScopeSelect = document.getElementById("recrawl-domain-scope");
  const recrawlExclusionsInput = document.getElementById("recrawl-exclusions");
  // Prefilled once, never on later status events — otherwise a re-render
  // mid-crawl would wipe out whatever is being typed into it.
  let recrawlExclusionsPrefilled = false;
  const prefillRecrawlExclusions = (exclusions) => {
    if (recrawlExclusionsPrefilled || !recrawlExclusionsInput) return;
    recrawlExclusionsPrefilled = true;
    if (exclusions) recrawlExclusionsInput.value = exclusions;
  };
  const recrawlLanguageInput = document.getElementById("recrawl-language");
  const printBtn = document.getElementById("print-btn");
  const exportCsvBtn = document.getElementById("export-csv-btn");
  const runDateEl = document.getElementById("run-date");
  const detectedLanguageNote = document.getElementById("detected-language-note");
  const blockedHostEl = document.getElementById("blocked-host-count");
  const estimatePanel = document.getElementById("estimate-panel");
  const estimatePagesFetchedEl = document.getElementById("estimate-pages-fetched");
  const estimateTotalPagesEl = document.getElementById("estimate-total-pages");
  const estimateAvgWordsEl = document.getElementById("estimate-avg-words");
  const estimateTotalWordsEl = document.getElementById("estimate-total-words");
  const estimateMessageEl = document.getElementById("estimate-message");
  const estimateConfidenceBadge = document.getElementById("estimate-confidence-badge");
  const estimateCmsPill = document.getElementById("estimate-cms-pill");
  const estimateConcurrencyPill = document.getElementById("estimate-concurrency-pill");
  const estimateDurationEl = document.getElementById("estimate-duration");
  const estimateSpeedEl = document.getElementById("estimate-speed");
  const queuedPanel = document.getElementById("queued-panel");
  const queuedMessageEl = document.getElementById("queued-message");
  const emailNotice = document.getElementById("email-notice");
  const proceedBtn = document.getElementById("proceed-btn");
  const adjustBtn = document.getElementById("adjust-btn");

  const shareBtn = document.getElementById("share-btn");
  const shareModal = document.getElementById("share-modal");
  const shareModalClose = document.getElementById("share-modal-close");
  const sharePublicToggle = document.getElementById("share-public-toggle");
  const shareLinkRow = document.getElementById("share-link-row");
  const shareLinkInput = document.getElementById("share-link-input");
  const shareCopyBtn = document.getElementById("share-copy-btn");
  const shareEmailForm = document.getElementById("share-email-form");
  const shareEmailInput = document.getElementById("share-email-input");
  const shareEmailStatus = document.getElementById("share-email-status");
  const peopleList = document.getElementById("people-list");
  const emailNoticeLabel = document.getElementById("email-notice-label");

  const pageMore = document.getElementById("page-more");
  const markdownStat = document.getElementById("markdown-stat");
  const markdownSizeEl = document.getElementById("markdown-size");
  const markdownPagesEl = document.getElementById("markdown-pages");
  const markdownBtn = document.getElementById("markdown-btn");
  const markdownNote = document.getElementById("markdown-note");
  const pageIssuesNote = document.getElementById("page-issues-note");
  const pageIssuesDetails = document.getElementById("page-issues-details");
  const pageIssuesList = document.getElementById("page-issues-list");
  const tryDifferentPageNote = document.getElementById("try-different-page-note");

  const setEmailNotice = (status) => {
    // Absent on a shared report — there's no owner in that view to email.
    if (!emailNotice) return;
    emailNoticeLabel.textContent = EMAILED_STATUSES.includes(status)
      ? "Summary emailed to"
      : "Summary will be emailed to";
    emailNotice.style.display = "inline";
  };

  const showTryDifferentPageNote = (totalWords, pageCount) => {
    // Only meaningful once a crawl has actually finished — total_words is
    // naturally 0 for the first moment of any crawl, not just a failed one.
    tryDifferentPageNote.style.display = (pageCount > 0 && totalWords === 0) ? "block" : "none";
  };

  const renderIssuesList = (issues) => {
    pageIssuesList.innerHTML = "";
    for (const p of issues) {
      const li = document.createElement("li");
      const urlSpan = document.createElement("span");
      urlSpan.className = "issue-url";
      urlSpan.textContent = p.url;
      const reasonSpan = document.createElement("span");
      reasonSpan.className = "issue-reason";
      reasonSpan.textContent = (p.blocked_by_host ? "Blocked: " : "Failed: ") + (p.error || "Unknown reason");
      li.append(urlSpan, reasonSpan);
      pageIssuesList.appendChild(li);
    }
  };

  const updateBlockedHostCount = (pages) => {
    const blocked = pages.filter((p) => p.blocked_by_host);
    const otherFailed = pages.filter((p) => !p.success && !p.blocked_by_host);
    setStatCount(blockedHostEl, blocked.length);

    // Deliberately no quoted error text here — some of those messages are
    // raw crawl4ai/Playwright internals (file paths, line numbers) that
    // are meaningless to a user. Full detail per page is one click away
    // under "View blocked/failed pages" in the same banner, where technical
    // text is fine since it's clearly a diagnostic list, not the headline.
    const lines = [];
    if (blocked.length) {
      lines.push(blocked.length === 1
        ? "1 page was blocked by this site's own bot detection."
        : `${blocked.length.toLocaleString("en-US")} pages were blocked by this site's own bot detection.`);
    }
    if (otherFailed.length) {
      lines.push(otherFailed.length === 1
        ? "1 page failed to load."
        : `${otherFailed.length.toLocaleString("en-US")} pages failed to load.`);
    }

    if (!lines.length) {
      pageIssuesDetails.style.display = "none";
      return;
    }
    pageIssuesNote.textContent = "";
    lines.forEach((line, i) => {
      if (i > 0) pageIssuesNote.appendChild(document.createElement("br"));
      pageIssuesNote.appendChild(document.createTextNode(line));
    });

    renderIssuesList([...blocked, ...otherFailed]);
    pageIssuesDetails.style.display = "block";
  };

  const showDetectedLanguage = (code) => {
    if (!code) return;
    detectedLanguageNote.textContent = "Auto-detected language: " + code;
    detectedLanguageNote.style.display = "block";
  };

  const updatePageCount = (count) => {
    pageCountEl.textContent = count.toLocaleString("en-US");
    pagesHeadingCountEl.textContent = count ? `— ${count.toLocaleString("en-US")} crawled` : "";
  };

  const updateSettingsPills = (domainScope, languageSetting, detectedLanguage, exclusions) => {
    const languages = languageSetting
      ? parseLanguageList(languageSetting)
      : detectedLanguage
        ? [detectedLanguage]
        : [];
    renderSettingsPills(domainScope, languages, !languageSetting && !!detectedLanguage, exclusions);
  };

  const CONFIDENCE_LABELS = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };

  const showEstimatePanel = (result) => {
    if (!result) return;
    estimatePagesFetchedEl.textContent = result.pages_fetched.toLocaleString("en-US");
    estimateTotalPagesEl.textContent = result.total_pages_estimate.toLocaleString("en-US");
    estimateAvgWordsEl.textContent = result.avg_words_per_page.toLocaleString("en-US");
    estimateTotalWordsEl.textContent = result.estimated_total_words.toLocaleString("en-US");

    estimateConfidenceBadge.textContent = CONFIDENCE_LABELS[result.confidence] || "";
    estimateConfidenceBadge.className = "pill confidence-badge confidence-" + (result.confidence || "low");

    if (result.detected_cms) {
      estimateCmsPill.textContent = result.detected_cms === "Contentful"
        ? "Detected platform: Contentful (headless — sitemap conventions vary)"
        : `Detected platform: ${result.detected_cms}`;
      estimateCmsPill.style.display = "";
    } else {
      estimateCmsPill.style.display = "none";
    }

    const pagesText = result.total_pages_estimate.toLocaleString("en-US");
    estimateMessageEl.textContent = result.sitemap_found
      ? `Found a sitemap — this site has approximately ${pagesText} pages. Crawling all of them may take a while.`
      : `No sitemap found — this estimate is based only on pages discovered so far (approximately ${pagesText}), so it may be less accurate. Crawling all of them may take a while.`;

    estimateDurationEl.textContent = formatDuration(result.estimated_duration_seconds);
    estimateSpeedEl.textContent =
      `${result.words_per_minute.toLocaleString("en-US")} words/min` +
      ` · ${result.pages_per_minute.toLocaleString("en-US")} pages/min`;
    estimateConcurrencyPill.textContent = concurrencyLabel(result.concurrent_crawls) + " server load";

    actionRow.style.display = "none";
    estimatePanel.style.display = "block";
  };

  const suggestedLanguage = suggestLanguageFromUrl(opts.sourceUrl);
  if (suggestedLanguage) recrawlLanguageInput.value = suggestedLanguage;

  printBtn.addEventListener("click", () => window.print());

  const deleteBtn = document.getElementById("delete-btn");
  if (deleteBtn) {
    // Only offered once there's a saved run to delete — mid-crawl the server
    // refuses anyway, since the crawler would re-save the row on finishing.
    if (opts.mode !== "live") deleteBtn.style.display = "";
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(`Delete this crawl of ${opts.sourceUrl}? This can't be undone.`)) return;
      deleteBtn.disabled = true;
      deleteBtn.textContent = "Deleting…";
      const res = await fetch("/crawl/" + opts.runId, { method: "DELETE" });
      if (res.ok) {
        window.location.href = "/";
        return;
      }
      const data = await res.json().catch(() => ({}));
      deleteBtn.disabled = false;
      deleteBtn.textContent = "Delete run";
      alert(data.detail || "Could not delete this crawl.");
    });
  }

  let currentPages = opts.initialPages || [];
  exportCsvBtn.addEventListener("click", () => {
    const host = (() => {
      try {
        return new URL(opts.sourceUrl).hostname;
      } catch (e) {
        return "crawl";
      }
    })();
    // A saved report only holds the first slice of rows, so the file is built
    // and streamed by the server. A live crawl isn't saved yet, so that one is
    // still assembled here from what has streamed in so far.
    if (opts.mode === "live") {
      downloadCsv(`${host}-word-count.csv`, pagesToCsv(currentPages));
    } else {
      window.location.href = `/crawl/${opts.runId}/export.csv`;
    }
  });

  // Same banner as updateBlockedHostCount(), but driven by the server's counts
  // instead of counting a pages array the saved report no longer holds.
  const applyIssueSummary = (summary) => {
    setStatCount(blockedHostEl, summary.blocked_count);
    const lines = [];
    if (summary.blocked_count) {
      lines.push(summary.blocked_count === 1
        ? "1 page was blocked by this site's own bot detection."
        : `${summary.blocked_count.toLocaleString("en-US")} pages were blocked by this site's own bot detection.`);
    }
    if (summary.failed_count) {
      lines.push(summary.failed_count === 1
        ? "1 page failed to load."
        : `${summary.failed_count.toLocaleString("en-US")} pages failed to load.`);
    }
    if (!lines.length) {
      pageIssuesDetails.style.display = "none";
      return;
    }
    pageIssuesNote.textContent = "";
    lines.forEach((line, i) => {
      if (i > 0) pageIssuesNote.appendChild(document.createElement("br"));
      pageIssuesNote.appendChild(document.createTextNode(line));
    });
    renderIssuesList(summary.issues);
    if (summary.issues_capped) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = `Showing the first ${summary.issues.length.toLocaleString("en-US")} — export the CSV for the rest.`;
      pageIssuesList.appendChild(li);
    }
    pageIssuesDetails.style.display = "block";
  };

  const MARKDOWN_STOP_REASONS = {
    stopped_run_cap: "the per-crawl size limit was reached",
    stopped_disk: "the server was running low on disk space",
    stopped_global_cap: "the server's total Markdown limit was reached",
    error: "of an error while saving",
  };

  // `terminal` gates the download: mid-crawl the archive would be a moving
  // target, and the button carries the size so the weight is known before the
  // click rather than after it starts.
  const applyMarkdown = (md, terminal) => {
    if (!md || !md.enabled) return;
    markdownStat.style.display = "";
    markdownSizeEl.textContent = formatBytes(md.bytes);
    markdownPagesEl.textContent = `${md.pages.toLocaleString("en-US")} pages`;

    if (markdownBtn) {
      markdownBtn.style.display = md.pages ? "" : "none";
      markdownBtn.disabled = !terminal;
      markdownBtn.textContent = terminal
        ? `Download Markdown · ${formatBytes(md.bytes)}`
        : "Download Markdown";
      markdownBtn.title = terminal ? "" : "Available when the crawl finishes";
    }

    const reason = MARKDOWN_STOP_REASONS[md.state];
    if (reason && terminal) {
      markdownNote.textContent =
        `Markdown was saved for the first ${md.pages.toLocaleString("en-US")} pages, then stopped because ` +
        `${reason}. The word count below is complete either way.`;
      markdownNote.style.display = "block";
    }
  };

  if (markdownBtn) {
    markdownBtn.addEventListener("click", () => {
      window.location.href = `/crawl/${opts.runId}/markdown.zip`;
    });
  }

  const initPagePaging = (total, shown) => {
    let loaded = shown;
    const note = document.getElementById("page-more-note");
    const btn = document.getElementById("page-more-btn");

    const update = () => {
      if (loaded >= total) {
        pageMore.style.display = total > PAGE_LIST_MAX ? "flex" : "none";
        btn.style.display = "none";
        return;
      }
      pageMore.style.display = "flex";
      if (loaded >= PAGE_LIST_MAX) {
        btn.style.display = "none";
        note.textContent =
          `Showing the first ${loaded.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} pages.` +
          " Export the CSV for the complete list.";
        return;
      }
      btn.style.display = "";
      btn.disabled = false;
      btn.textContent = "Show more";
      note.textContent = `${loaded.toLocaleString("en-US")} of ${total.toLocaleString("en-US")}`;
    };

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Loading…";
      const limit = Math.min(500, PAGE_LIST_MAX - loaded);
      try {
        const res = await fetch(`/crawl/${opts.runId}/pages?offset=${loaded}&limit=${limit}`);
        const data = await res.json();
        for (const page of data.pages) renderPageRow(tbody, page, true);
        loaded += data.pages.length;
      } catch (e) {
        note.textContent = "Couldn't load more pages.";
      }
      update();
    });

    update();
  };

  const setStatus = (status) => {
    statusBadge.className = badgeClass(status);
    statusText.textContent = statusLabel(status);
    statusDot.innerHTML = status === "crawling" ? '<span class="pulse-dot"></span>' : "";
  };

  const showLimitNote = () => {
    limitNote.textContent = "The configured page limit was reached — the crawl may be incomplete.";
    limitNote.style.display = "block";
  };

  const showError = (message) => {
    errorBox.textContent = message || "The crawl failed.";
    errorBox.style.display = "block";
  };

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Cancelling…";
    await fetch("/crawl/" + opts.runId + "/cancel", { method: "POST" });
    // The actual status change arrives via the SSE "status" event below.
  });

  recrawlForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    recrawlBtn.disabled = true;
    recrawlBtn.textContent = "Starting…";
    const res = await fetch("/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: opts.sourceUrl,
        domain_scope: recrawlDomainScopeSelect.value,
        language: recrawlLanguageInput.value.trim() || null,
        exclusions: recrawlExclusionsInput.value.trim() || null,
        force_recrawl: true,
        capture_markdown: document.getElementById("recrawl-markdown").checked,
      }),
    });
    const data = await res.json();
    window.location.href = "/crawl/" + data.run_id;
  });

  if (shareBtn) {
    const shareUrl = window.location.origin + "/share/" + opts.runId;
    let isPublic = !!opts.initialIsPublic;

    const renderPublicState = (pub) => {
      isPublic = pub;
      sharePublicToggle.setAttribute("aria-checked", pub ? "true" : "false");
      shareLinkInput.value = shareUrl;
      shareLinkRow.style.display = pub ? "flex" : "none";
    };

    const renderPeople = (recipients) => {
      // The owner row is server-rendered; everything after it is an invite.
      while (peopleList.children.length > 1) peopleList.lastElementChild.remove();
      for (const person of recipients || []) {
        const li = document.createElement("li");
        li.className = "person-row";

        const avatar = document.createElement("span");
        avatar.className = "avatar";
        avatar.textContent = (person.email[0] || "?").toUpperCase();

        const id = document.createElement("div");
        id.className = "person-id";
        const name = document.createElement("span");
        name.className = "person-name";
        name.textContent = person.email;
        const sub = document.createElement("span");
        sub.className = "person-email";
        sub.textContent = person.created_at ? "Invited " + formatDate(person.created_at) : "Invited";
        id.append(name, sub);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "person-remove";
        remove.textContent = "×";
        remove.title = "Remove " + person.email;
        remove.setAttribute("aria-label", "Remove " + person.email);
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          const res = await fetch(
            "/crawl/" + opts.runId + "/share/recipients?email=" + encodeURIComponent(person.email),
            { method: "DELETE" },
          );
          if (res.ok) {
            const data = await res.json();
            renderPeople(data.recipients);
          } else {
            remove.disabled = false;
          }
        });

        li.append(avatar, id, remove);
        peopleList.appendChild(li);
      }
    };

    const openShareModal = () => {
      shareModal.hidden = false;
      shareEmailStatus.textContent = "";
      shareEmailInput.focus();
    };
    const closeShareModal = () => {
      shareModal.hidden = true;
      shareBtn.focus();
    };

    shareBtn.addEventListener("click", openShareModal);
    shareModalClose.addEventListener("click", closeShareModal);
    shareModal.addEventListener("click", (evt) => {
      if (evt.target === shareModal) closeShareModal();
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape" && !shareModal.hidden) closeShareModal();
    });

    sharePublicToggle.addEventListener("click", async () => {
      sharePublicToggle.disabled = true;
      try {
        const res = await fetch("/crawl/" + opts.runId + "/share", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_public: !isPublic }),
        });
        const data = await res.json();
        renderPublicState(data.is_public);
      } finally {
        sharePublicToggle.disabled = false;
      }
    });

    shareCopyBtn.addEventListener("click", async () => {
      shareLinkInput.select();
      await navigator.clipboard.writeText(shareLinkInput.value);
      shareCopyBtn.textContent = "Copied!";
      setTimeout(() => { shareCopyBtn.textContent = "Copy link"; }, 1500);
    });

    shareEmailForm.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const sendBtn = shareEmailForm.querySelector("button");
      sendBtn.disabled = true;
      shareEmailStatus.textContent = "Sending…";
      try {
        const res = await fetch("/crawl/" + opts.runId + "/share/email", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: shareEmailInput.value.trim() }),
        });
        if (res.ok) {
          const data = await res.json();
          // Inviting someone turns the link on server-side — reflect that here
          // rather than leaving the switch showing a stale "private".
          renderPublicState(data.is_public);
          renderPeople(data.recipients);
          shareEmailInput.value = "";
          shareEmailStatus.textContent = "Sent!";
        } else {
          shareEmailStatus.textContent = "Couldn't send — try again.";
        }
      } finally {
        sendBtn.disabled = false;
      }
    });

    renderPublicState(isPublic);
    renderPeople(opts.initialShareRecipients);
    // Sharing a half-finished report would hand out a link to partial results.
    if (opts.mode === "live") shareBtn.disabled = true;
  }

  if (opts.mode === "past" || opts.mode === "shared") {
    if (opts.mode === "shared") {
      actionRow.style.display = "none";
    }
    runDateEl.textContent = "Started: " + formatDate(opts.createdAt);
    setEmailNotice(opts.initialStatus);
    setStatus(opts.initialStatus);
    totalWordsEl.textContent = opts.initialTotalWords.toLocaleString("en-US");
    updatePageCount(opts.initialPageCount);
    setStatCount(loginBlockedEl, opts.initialLoginBlockedCount || 0);
    setStatCount(duplicateEl, opts.initialDuplicateCount || 0);
    renderSettingsPills(opts.domainScope, parseLanguageList(opts.languageSetting), opts.languageAutoDetected, opts.exclusions);
    prefillRecrawlExclusions(opts.exclusions);
    if (opts.initialLimitReached) showLimitNote();
    if (opts.initialStatus === "failed") showError();
    if (opts.initialStatus === "cancelled") cancelNote.style.display = "block";
    if (opts.initialStatus === "paused") pausedPastNote.style.display = "block";
    showTryDifferentPageNote(opts.initialTotalWords, opts.initialPageCount);

    // A saved report is handed totals the server already grouped plus the first
    // slice of rows — never every page. See app/report.py for why.
    const initialPages = opts.initialPages || [];
    for (const page of initialPages) renderPageRow(tbody, page, true);
    applyIssueSummary(opts.summary);
    applyMarkdown(opts.initialMarkdown, true);
    renderServerSummary(opts.summary);
    initPagePaging(opts.summary.total_pages, initialPages.length);
    return;
  }

  // live mode
  runDateEl.textContent = "Started: " + formatDate(opts.startedAt);
  setStatCount(blockedHostEl, 0);
  const seenUrls = new Set();
  const pages = [];
  currentPages = pages; // same array reference, kept in sync as pages.push() happens below

  const applyStatus = (data) => {
    setStatus(data.status);
    setEmailNotice(data.status);
    totalWordsEl.textContent = data.total_words.toLocaleString("en-US");
    updatePageCount(data.page_count);
    setStatCount(loginBlockedEl, data.login_blocked_count);
    setStatCount(duplicateEl, data.duplicate_count);
    showDetectedLanguage(data.detected_language);
    updateSettingsPills(data.domain_scope, data.language_setting, data.detected_language, data.exclusions);
    prefillRecrawlExclusions(data.exclusions);
    if (data.limit_reached) showLimitNote();
    if (data.status === "failed") showError(data.error);
    if (data.status === "cancelled") {
      cancelNote.textContent = data.stopped_reason || "This crawl was cancelled — showing partial results.";
      cancelNote.style.display = "block";
    }
    applyMarkdown(data.markdown, TERMINAL_STATUSES.includes(data.status));
    if (TERMINAL_STATUSES.includes(data.status)) {
      cancelBtn.style.display = "none";
      queuedPanel.style.display = "none";
      if (shareBtn) shareBtn.disabled = false;
      showTryDifferentPageNote(data.total_words, data.page_count);
      if (data.status === "paused") {
        showEstimatePanel(data.estimate_result);
      }
    } else {
      cancelBtn.style.display = "";
      if (data.status === "queued") {
        queuedMessageEl.textContent = data.queue_position
          ? `You're #${data.queue_position} in line — this server is busy right now. This will start automatically, and we'll email you when it's done.`
          : "This will start automatically, and we'll email you when it's done.";
        queuedPanel.style.display = "block";
      } else {
        queuedPanel.style.display = "none";
      }
    }
  };

  // Render the job's REAL current status immediately, before ever
  // connecting to SSE — previously this always hardcoded "starting"
  // regardless of the actual status, so reloading a page that had already
  // paused (or finished, or failed) misleadingly showed "Crawling" until
  // the SSE replay caught up moments later.
  if (opts.initialStatusPayload) {
    applyStatus(opts.initialStatusPayload);
  } else {
    setStatus("starting");
    setEmailNotice("starting");
    cancelBtn.style.display = "";
  }

  proceedBtn.addEventListener("click", async () => {
    proceedBtn.disabled = true;
    proceedBtn.textContent = "Resuming…";
    const estimateExclusionsEl = document.getElementById("estimate-exclusions");
    await fetch("/crawl/" + opts.runId + "/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // null, not "", so an untouched field means "keep what the crawl already
      // had" rather than "clear the exclusions".
      body: JSON.stringify({ exclusions: (estimateExclusionsEl.value || "").trim() || null }),
    });
    estimatePanel.style.display = "none";
    actionRow.style.display = "";
    cancelBtn.style.display = "";
    setStatus("crawling");
    connectEvents();
  });

  adjustBtn.addEventListener("click", () => {
    window.location.href = "/";
  });

  function connectEvents() {
    const source = new EventSource("/events/" + opts.runId);

    source.addEventListener("page", (evt) => {
      const data = JSON.parse(evt.data);
      // A reconnect after "Proceed with crawl" replays every page already
      // known before streaming new ones — skip anything already counted.
      if (seenUrls.has(data.page.url)) return;
      seenUrls.add(data.page.url);
      pages.push(data.page);
      // Stop growing the DOM past the cap — the counts and the summary keep
      // using every page, but a long crawl shouldn't end up with a row per
      // page in the table while you're watching it.
      if (pages.length <= PAGE_LIST_MAX) {
        renderPageRow(tbody, data.page);
      } else if (pages.length === PAGE_LIST_MAX + 1) {
        pageMore.style.display = "flex";
        document.getElementById("page-more-btn").style.display = "none";
        document.getElementById("page-more-note").textContent =
          `Showing the most recent ${PAGE_LIST_MAX.toLocaleString("en-US")} pages while this crawl runs.`;
      }
      totalWordsEl.textContent = data.total_words.toLocaleString("en-US");
      updatePageCount(pages.length);
      updateBlockedHostCount(pages);
      if (data.markdown_bytes !== undefined) {
        applyMarkdown({ enabled: true, bytes: data.markdown_bytes, pages: data.markdown_pages, state: "capturing" }, false);
      }
    });

    source.addEventListener("login_blocked", (evt) => {
      const data = JSON.parse(evt.data);
      // Intentionally not rendered in the page list — just a running count of
      // pages that turned out to be login walls rather than real content.
      setStatCount(loginBlockedEl, data.login_blocked_count);
    });

    source.addEventListener("duplicate", (evt) => {
      const data = JSON.parse(evt.data);
      // Same reasoning as login_blocked: the page was fetched but is the same
      // content as one already counted, so it belongs in neither the list nor
      // the word total — only in this counter.
      setStatCount(duplicateEl, data.duplicate_count);
    });

    source.addEventListener("status", (evt) => {
      const data = JSON.parse(evt.data);
      applyStatus(data);
      if (TERMINAL_STATUSES.includes(data.status)) {
        renderSummary(pages);
        source.close();
      }
    });

    source.onerror = () => {
      // EventSource auto-retries; if the job is already gone server-side this
      // will just keep failing quietly, which is fine for a local dev tool.
    };

    return source;
  }

  connectEvents();
}
