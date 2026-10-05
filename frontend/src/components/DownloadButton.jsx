// A button that downloads a file in place: no new tab, a "Preparing..." state while the server builds it (RCA and
// periodic reports are generated on request), and a clear message if it fails. Replaces <a target="_blank"> links,
// which opened a tab that flashed and closed.
import { useState } from "react";
import { downloadFile } from "../api/api";
import { describeApiError } from "../utils/download";

export default function DownloadButton({ path, fallbackName, className = "", title, children }) {
  const [state, setState] = useState("idle");        // idle | busy | error
  const [message, setMessage] = useState("");

  async function onClick(e) {
    e.preventDefault();
    e.stopPropagation();                             // table rows expand on click
    if (state === "busy") return;
    setState("busy"); setMessage("");
    try {
      await downloadFile(path, fallbackName);
      setState("idle");
    } catch (err) {
      setMessage(describeApiError(err, "preparing the download"));
      setState("error");
      setTimeout(() => setState(s => (s === "error" ? "idle" : s)), 8000);
    }
  }

  return (
    <span className="dl-wrap">
      <button type="button" className={className} onClick={onClick} disabled={state === "busy"} aria-busy={state === "busy"} title={title}>
        {state === "busy" ? "Preparing\u2026" : children}
      </button>
      {state === "error" && <span role="alert" className="dl-err">{message}</span>}
    </span>
  );
}
