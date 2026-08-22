const form = document.getElementById("cache-form");
const input = document.getElementById("url");
const submit = document.getElementById("submit");
const statusNode = document.getElementById("status");
const result = document.getElementById("result");
const resultUrl = document.getElementById("result-url");
const copy = document.getElementById("copy");

let pollTimer = null;
let finalUrl = "";

function setStatus(message) { statusNode.textContent = message; }
function stopPolling() { if (pollTimer) clearTimeout(pollTimer); pollTimer = null; }

async function getJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `リクエストに失敗しました (${response.status})`);
  return data;
}

async function poll(videoId) {
  try {
    const data = await getJson(`/api/status/${encodeURIComponent(videoId)}`);
    if (data.status === "done") {
      finalUrl = data.url;
      resultUrl.textContent = finalUrl;
      result.hidden = false;
      submit.disabled = false;
      setStatus("完了しました。URLをVideoTXLなどへ貼り付けてください。");
      return;
    }
    if (data.status === "error") {
      submit.disabled = false;
      setStatus(`失敗: ${data.error || "原因不明"}`);
      return;
    }
    if (data.status === "unknown") {
      submit.disabled = false;
      setStatus("ジョブ情報がありません。もう一度実行してください。");
      return;
    }
    setStatus(data.status === "queued" ? "処理待ちです…" : "処理中です…（初回は数分かかることがあります）");
    pollTimer = setTimeout(() => poll(videoId), 3000);
  } catch (error) {
    submit.disabled = false;
    setStatus(`状態確認に失敗: ${error.message}`);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  stopPolling();
  result.hidden = true;
  submit.disabled = true;
  setStatus("送信中…");
  try {
    const data = await getJson("/api/cache", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value }),
    });
    if (data.status === "done") {
      finalUrl = data.url;
      resultUrl.textContent = finalUrl;
      result.hidden = false;
      submit.disabled = false;
      setStatus("すでにキャッシュ済みです。");
      return;
    }
    setStatus(data.status === "queued" ? "処理待ちです…" : "処理中です…");
    poll(data.video_id);
  } catch (error) {
    submit.disabled = false;
    setStatus(`開始できませんでした: ${error.message}`);
  }
});

copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(finalUrl);
    setStatus("再生用URLをコピーしました。");
  } catch {
    setStatus("コピーできませんでした。URLを選択してコピーしてください。");
  }
});
