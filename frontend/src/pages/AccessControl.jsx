// src/pages/AccessControl.jsx
//
// Single home for everything about WHO can do WHAT: users, groups, roles &
// permissions, scoped bindings/overrides and access reviews. Replaces the
// two overlapping pages (User Management + RBAC Administration), which
// each showed roles, permissions and groups in their own way.
//
// Deep-linkable: /access/users, /access/groups, /access/roles,
// /access/advanced, /access/reviews.
import { useMemo } from "react";
import { NavLink, Navigate, useParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { UsersIcon, LayersIcon, ShieldIcon, KeyIcon, ClipboardIcon } from "../components/icons";
import { ToastProvider } from "./access/ui";
import UsersTab from "./access/UsersTab";
import GroupsTab from "./access/GroupsTab";
import RolesTab from "./access/RolesTab";
import AdvancedTab from "./access/AdvancedTab";
import ReviewsTab from "./access/ReviewsTab";
import "./AccessControl.css";

export default function AccessControl() {
  const { hasPermission } = useAuth();
  const { tab } = useParams();

  const tabs = useMemo(() => [
    { key: "users",    label: "Users",             icon: UsersIcon,     show: hasPermission("users.view"),  el: <UsersTab /> },
    { key: "groups",   label: "Groups",            icon: LayersIcon,    show: hasPermission("groups.view"), el: <GroupsTab /> },
    { key: "roles",    label: "Roles & permissions", icon: ShieldIcon,  show: hasPermission("roles.view"),  el: <RolesTab /> },
    { key: "advanced", label: "Scopes & overrides", icon: KeyIcon,      show: hasPermission("rbac.scope.view") || hasPermission("rbac.binding.view") || hasPermission("rbac.override.manage"), el: <AdvancedTab /> },
    { key: "reviews",  label: "Access reviews",    icon: ClipboardIcon, show: hasPermission("rbac.review.conduct"), el: <ReviewsTab /> },
  ].filter((t) => t.show), [hasPermission]);

  if (tabs.length === 0) return <Navigate to="/overview" replace />;
  const current = tabs.find((t) => t.key === tab);
  if (!current) return <Navigate to={`/access/${tabs[0].key}`} replace />;

  return (
    <ToastProvider>
      <div className="ac-page">
        <div className="ac-pagehead">
          <h1>Access <span className="accent">Control</span></h1>
          <p className="muted">Manage who can sign in, what they can do, and which accounts they can see.</p>
        </div>
        <nav className="ac-tabs" aria-label="Access control sections">
          {tabs.map((t) => (
            <NavLink key={t.key} to={`/access/${t.key}`} className={({ isActive }) => `ac-tab${isActive ? " on" : ""}`}>
              <t.icon size={14} /> {t.label}
            </NavLink>
          ))}
        </nav>
        <div className="ac-content" key={current.key}>{current.el}</div>
      </div>
    </ToastProvider>
  );
}
