function getBaseUrl() {
  return document.getElementById("baseUrl").value.replace(/\/+$/, "");
}

const backendSelect = document.getElementById("backendSelect");
const baseUrlInput = document.getElementById("baseUrl");

backendSelect.addEventListener("change", () => {
  const value = backendSelect.value;
  if (value !== "custom") {
    baseUrlInput.value = value;
  }
});

baseUrlInput.addEventListener("input", () => {
  if (backendSelect.value === "custom") {
    return;
  }
  const match = Array.from(backendSelect.options).some(
    (opt) => opt.value === baseUrlInput.value
  );
  backendSelect.value = match ? baseUrlInput.value : "custom";
});

function parseFilterIds() {
  const raw = document.getElementById("filterIds").value.trim();
  if (!raw) return [];
  return raw
    .split(",")
    .map((x) => x.trim())
    .filter((x) => x.length > 0)
    .map((x) => Number(x))
    .filter((x) => Number.isFinite(x));
}

function parseCsvIds(text) {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return [];
  const delimiter = lines[0].includes(";") ? ";" : ",";
  const header = lines[0].split(delimiter).map((h) => h.trim().toLowerCase());
  let startIdx = 0;
  let idCol = 0;
  if (header.some((h) => /[a-z]/.test(h))) {
    const known = ["dhlabid", "urn_seq", "book_id"];
    for (const k of known) {
      const idx = header.indexOf(k);
      if (idx !== -1) {
        idCol = idx;
        break;
      }
    }
    startIdx = 1;
  }
  const ids = [];
  for (let i = startIdx; i < lines.length; i++) {
    const cols = lines[i].split(delimiter);
    if (!cols.length) continue;
    const val = (cols[idCol] || "").trim();
    const num = Number(val);
    if (Number.isFinite(num)) ids.push(num);
  }
  return ids;
}

document.getElementById("loadCorpusBtn").addEventListener("click", async () => {
  const fileInput = document.getElementById("corpusFile");
  const status = document.getElementById("corpusStatus");
  if (!fileInput.files || !fileInput.files[0]) {
    status.textContent = "No file selected.";
    return;
  }
  const file = fileInput.files[0];
  let ids = [];
  if (file.name.toLowerCase().endsWith(".xlsx") || file.name.toLowerCase().endsWith(".xls")) {
    const data = new Uint8Array(await file.arrayBuffer());
    const workbook = XLSX.read(data, { type: "array" });
    const sheetName = workbook.SheetNames[0];
    const sheet = workbook.Sheets[sheetName];
    const rows = XLSX.utils.sheet_to_json(sheet, { header: 1 });
    if (rows.length) {
      const header = rows[0].map((h) => String(h).trim().toLowerCase());
      let startIdx = 0;
      let idCol = 0;
      if (header.some((h) => /[a-z]/.test(h))) {
        const known = ["dhlabid", "urn_seq", "book_id"];
        for (const k of known) {
          const idx = header.indexOf(k);
          if (idx !== -1) {
            idCol = idx;
            break;
          }
        }
        startIdx = 1;
      }
      for (let i = startIdx; i < rows.length; i++) {
        const val = rows[i][idCol];
        const num = Number(val);
        if (Number.isFinite(num)) ids.push(num);
      }
    }
  } else {
    const text = await file.text();
    ids = parseCsvIds(text);
  }
  if (!ids.length) {
    status.textContent = "No IDs found in file.";
    return;
  }
  document.getElementById("filterIds").value = ids.join(",");
  document.getElementById("useFilter").checked = true;
  status.textContent = `Loaded ${ids.length} IDs`;
});

function getCommonPayload() {
  const docSamplesRaw = document.getElementById("docSamples").value.trim();
  const docSamples = docSamplesRaw.length ? Number(docSamplesRaw) : null;
  return {
    schema: document.getElementById("schema").value.trim() || "unigrams",
    useFilter: document.getElementById("useFilter").checked,
    filterIds: parseFilterIds(),
    docSamples: Number.isFinite(docSamples) ? docSamples : null,
  };
}

async function request(path, payload) {
  const start = performance.now();
  const res = await fetch(`${getBaseUrl()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || data.detail || "Request failed");
  }
  return { data, ms: performance.now() - start };
}

document.getElementById("healthBtn").addEventListener("click", async () => {
  const status = document.getElementById("healthStatus");
  status.textContent = "…";
  try {
    const start = performance.now();
    const res = await fetch(`${getBaseUrl()}/health`);
    const data = await res.json();
    const ms = performance.now() - start;
    status.textContent = `${data.status} (${data.version}) in ${ms.toFixed(1)} ms`;
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  }
});

document.getElementById("concordanceBtn").addEventListener("click", async () => {
  const out = document.getElementById("concordanceOut");
  out.textContent = "Running…";
  const payload = {
    ...getCommonPayload(),
    wordA: document.getElementById("wordA").value.trim(),
    wordB: document.getElementById("wordB").value.trim(),
    window: Number(document.getElementById("window").value),
    before: Number(document.getElementById("before").value),
    after: Number(document.getElementById("after").value),
    perBook: Number(document.getElementById("perBook").value),
    totalLimit: Number(document.getElementById("totalLimit").value),
    symmetric: document.getElementById("symmetric").checked,
    excludeSelf: document.getElementById("excludeSelf").checked,
  };
  try {
    const { data, ms } = await request("/concordance", payload);
    out.textContent = data.rows
      .map((r) => `${r.bookId} @ ${r.pos}: ${r.frag}`)
      .join("\n");
    out.textContent += `\n\nTime: ${ms.toFixed(1)} ms`;
  } catch (err) {
    out.textContent = `error: ${err.message}`;
  }
});

document.getElementById("freqBtn").addEventListener("click", async () => {
  const out = document.getElementById("freqOut");
  out.textContent = "Running…";
  const fragOut = document.getElementById("freqFragOut");
  fragOut.textContent = "";
  const payload = {
    ...getCommonPayload(),
    wordA: document.getElementById("freqA").value.trim(),
    wordB: document.getElementById("freqB").value.trim(),
    window: Number(document.getElementById("freqWindow").value),
    symmetric: document.getElementById("freqSym").checked,
    excludeSelf: document.getElementById("freqExcludeSelf").checked,
  };
  try {
    const { data, ms } = await request("/near_frequency", payload);
    out.textContent = `Total: ${data.total}\nDocs: ${data.docs}\nTime: ${ms.toFixed(
      1
    )} ms`;
  } catch (err) {
    out.textContent = `error: ${err.message}`;
  }
});

document.getElementById("freqSampleBtn").addEventListener("click", async () => {
  const out = document.getElementById("freqFragOut");
  out.textContent = "Running…";
  const window = Number(document.getElementById("freqFragWindow").value);
  const payload = {
    ...getCommonPayload(),
    wordA: document.getElementById("freqA").value.trim(),
    wordB: document.getElementById("freqB").value.trim(),
    window,
    before: window,
    after: window,
    perBook: 2,
    totalLimit: 50,
    symmetric: document.getElementById("freqSym").checked,
    excludeSelf: document.getElementById("freqExcludeSelf").checked,
  };
  try {
    const { data, ms } = await request("/concordance", payload);
    out.textContent = data.rows
      .map((r) => `${r.bookId} @ ${r.pos}: ${r.frag}`)
      .join("\n");
    out.textContent += `\n\nTime: ${ms.toFixed(1)} ms`;
  } catch (err) {
    out.textContent = `error: ${err.message}`;
  }
});

document.getElementById("collBtn").addEventListener("click", async () => {
  const out = document.getElementById("collOut");
  out.textContent = "Running…";
  const payload = {
    ...getCommonPayload(),
    word: document.getElementById("collWord").value.trim(),
    before: Number(document.getElementById("collBefore").value),
    after: Number(document.getElementById("collAfter").value),
    perBook: Number(document.getElementById("collPerBook").value),
  };
  try {
    const { data, ms } = await request("/collocations", payload);
    const total = data.rows.reduce((acc, r) => acc + r.count, 0);
    out.textContent = [
      `Top ${data.rows.length} collocations (sampled)`,
      `Total sample hits: ${total}`,
      `Time: ${ms.toFixed(1)} ms`,
      "",
      data.rows.map((r) => `${r.word}\t${r.count}`).join("\n"),
    ].join("\n");
  } catch (err) {
    out.textContent = `error: ${err.message}`;
  }
});
