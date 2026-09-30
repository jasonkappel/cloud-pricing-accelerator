import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";

import {
  getMe,
  listApplications,
  SIGN_OUT_URL,
  usesPlatformSignIn,
  type ApplicationSummary,
  type SignedInUser,
} from "../api";
import { needsYou } from "../pages/homeParts";

function navClass({ isActive }: { isActive: boolean }) {
  return isActive ? "cp-nav-link active" : "cp-nav-link";
}

export function Nav() {
  const location = useLocation();
  const [applications, setApplications] = useState<ApplicationSummary[] | null>(null);
  const [user, setUser] = useState<SignedInUser | null>(null);
  const [canSignOut, setCanSignOut] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getMe(controller.signal)
      .then(setUser)
      .catch(() => {
        if (!controller.signal.aborted) {
          setUser(null);
        }
      });
    usesPlatformSignIn()
      .then((value) => {
        if (!controller.signal.aborted) {
          setCanSignOut(value);
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    listApplications(controller.signal)
      .then(setApplications)
      .catch(() => {
        if (!controller.signal.aborted) {
          setApplications(null);
        }
      });
    return () => controller.abort();
  }, [location.pathname]);

  const waiting = applications?.filter(needsYou).length ?? 0;
  const total = applications?.length ?? 0;

  return (
    <aside className="cp-nav no-print" aria-label="Workspace">
      <div className="cp-brand">
        <span aria-hidden="true" className="cp-brand-mark">CP</span>
        <div>
          <strong>Cloud Pricing Accelerator</strong>
          <span>Public list prices</span>
        </div>
      </div>
      <NavLink className="cp-button cp-button-primary cp-new-estimate" to="/estimates/new/mode">
        <span aria-hidden="true">+</span> New estimate
      </NavLink>
      <nav aria-label="Primary navigation" className="cp-nav-group">
        <p className="cp-nav-heading">Workspace</p>
        <NavLink className={navClass} end to="/">
          <span>Home</span>
          {waiting > 0 && <span className="cp-count" aria-label={`${waiting} need you`}>{waiting}</span>}
        </NavLink>
        <NavLink className={navClass} to="/estimates">
          <span>Estimates</span>
          {total > 0 && <span className="cp-count" aria-label={`${total} estimates`}>{total}</span>}
        </NavLink>
        <NavLink className={navClass} to="/price-book">
          <span>Price book</span>
        </NavLink>
      </nav>
      <div className="cp-nav-foot">
        <NavLink className={navClass} to="/settings">
          <span>Settings</span>
        </NavLink>
        {user && (
          <div className="cp-nav-user" aria-label="Signed in">
            <span className="cp-nav-user-name">{user.name}</span>
            <span className="cp-nav-user-roles">
              {user.roles.length > 0 ? user.roles.join(", ") : "No roles assigned"}
            </span>
            {canSignOut && (
              <a className="cp-nav-user-signout" href={SIGN_OUT_URL}>Sign out</a>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
