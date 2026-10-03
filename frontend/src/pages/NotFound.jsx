// Shown for any URL inside the app that matches no route (audit B1). Previously every unknown
// path silently redirected to /overview, so a mistyped or outdated link looked like it worked and
// could not be debugged. The attempted path is shown so it can be reported or corrected.
import { Link, useLocation } from "react-router-dom";
import { EmptyState } from "../components/ui";

export default function NotFound() {
  const { pathname, search } = useLocation();
  return (
    <div className="page" role="alert" style={{ padding: "48px 24px" }}>
      <EmptyState
        title="Page not found"
        body={<>There is no page at <code style={{ overflowWrap: "anywhere" }}>{pathname}{search}</code>. The link may be mistyped or out of date.</>}
        action={<div style={{ marginTop: 16 }}><Link className="ui-btn" to="/overview">Go to Overview</Link></div>}
      />
    </div>
  );
}
