import { lazy, Suspense, useState, useEffect } from "react";

// Build separate chunks for the two real frontends. The exhibition must not
// load workbench diagnostics before the user opens the workbench.
const ExhibitView = lazy(() => import("./exhibit/exhibit-view").then(m => ({default: m.ExhibitView})));
const Workbench = lazy(() => import("./workbench").then(m => ({default: m.Workbench})));

export function App() {
  const [mode, setMode] = useState<"cockpit" | "workbench">(() => {
    if (typeof window !== "undefined") {
      const param = new URLSearchParams(window.location.search).get("view");
      if (param === "workbench") return "workbench";
      if (param === "cockpit") return "cockpit";
    }
    return "cockpit"; // Default view: Cockpit
  });

  const [currentRobotId, setCurrentRobotId] = useState("annie-a");

  useEffect(() => {
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("view", mode);
      window.history.replaceState({}, "", url.toString());
    }
  }, [mode]);

  return <Suspense fallback={<div role="status">页面加载中…</div>}>
    {mode === "cockpit" ? (
      <ExhibitView
        currentRobotId={currentRobotId}
        onSelectRobot={setCurrentRobotId}
        onSwitchMode={setMode}
      />
    ) : <Workbench onSwitchMode={setMode} />}
  </Suspense>;
}
