"use client";

import {Suspense} from "react";
import Dashboard from "./Dashboard";

function DashboardLoading(){
  return <main className="dashboard-loading" aria-busy="true" aria-label="Loading HomeCam">
    <div className="skeleton skeleton-header"/>
    <div className="skeleton skeleton-hero"/>
    <div className="skeleton-grid">
      <div className="skeleton skeleton-camera"/>
      <div className="skeleton skeleton-camera"/>
    </div>
  </main>;
}

export default function Home(){
  return <Suspense fallback={<DashboardLoading/>}>
    <Dashboard/>
  </Suspense>;
}
