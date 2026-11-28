import React, { useEffect, useState } from "react";
import { api } from "../api";

const MODES = ["失败硬退", "挂缺口可核"];

function modeLabel(mode) {
  return mode === "挂缺口可核" ? "挂缺口可核" : mode || "—";
}

export default function Policy() {
  const [policy, setPolicy] = useState(null);
  const [flips, setFlips] = useState([]);
  const [mode, setMode] = useState("失败硬退");
  const [hold, setHold] = useState(20);
  const [flash, setFlash] = useState(null);

  const load = () => {
    api.policy().then((p) => {
      setPolicy(p);
      setMode(p.mode);
      setHold(p.hold_sec);
    });
    api.policyFlips().then(setFlips);
  };
  useEffect(() => {
    load();
  }, []);

  const dirty = policy && (policy.mode !== mode || Number(policy.hold_sec) !== Number(hold));

  const flip = () => {
    api.setPolicy({ mode, hold_sec: hold }).then((res) => {
      setFlash(res);
      load();
    });
  };

  return (
    <div className="stack">
      <section className="panel">
        <h1>回退拨</h1>
        {/* 口径已在服务端钉死：在途跟现档（带痕），已收口跟冻结节（只读）。此处仅作固定说明，不再弹出问答。 */}
        <p className="muted">
          单一真源：本拨杆。在途单（写闪中）随拧档同步到现档并留跟随痕；已收口单冻结开收口当刻的回退档，事后拧档不改写。
        </p>
        <div className="row">
          <span className="muted mono">现档</span>
          <strong className="cyan mono">
            {policy ? `${modeLabel(policy.mode)} / ${policy.hold_sec}s` : "—"}
          </strong>
          <span className="muted mono">v{policy ? policy.version : "—"}</span>
        </div>
        <div className="row">
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            {MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <input type="number" value={hold} onChange={(e) => setHold(Number(e.target.value))} />
          <span className="muted mono">hold_sec</span>
          <button className="btn" disabled={!dirty} onClick={flip}>
            保存拨杆
          </button>
          {flash && (
            <span className={`mono ${flash.changed ? "cyan" : "muted"}`}>
              {flash.changed
                ? `已拧到 v${flash.policy.version}，随单 ${flash.affected} 张`
                : "档位未变"}
            </span>
          )}
        </div>
      </section>

      <section className="panel">
        <h2>拧档流水</h2>
        <p className="muted">每次拧档的版本前后与随单数；逐单跟随痕可重放「哪一单跟了哪一版」。</p>
        <table className="table">
          <thead>
            <tr>
              <th>#</th>
              <th>时</th>
              <th>档位</th>
              <th>版本</th>
              <th>随单</th>
              <th>跟随痕</th>
            </tr>
          </thead>
          <tbody>
            {flips.length === 0 ? (
              <tr>
                <td colSpan={6} className="muted">
                  尚未拧档
                </td>
              </tr>
            ) : (
              flips.map((f) => (
                <tr key={f.id}>
                  <td>{f.id}</td>
                  <td className="mono">{f.at}</td>
                  <td className="mono">
                    {modeLabel(f.from_mode)}→{modeLabel(f.to_mode)}
                  </td>
                  <td className="mono">
                    v{f.version_before}→v{f.version_after}
                  </td>
                  <td>{f.affected_orders}</td>
                  <td className="mono">
                    {f.跟随.length === 0
                      ? "—"
                      : f.跟随.map((t) => `#${t.order_id}${t.outcome}`).join("，")}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
