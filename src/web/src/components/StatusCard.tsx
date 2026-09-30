import type { ReactNode } from "react";

interface StatusCardProps {
  title: string;
  value: string;
  children: ReactNode;
}

export function StatusCard({ title, value, children }: StatusCardProps) {
  return (
    <article className="card">
      <p className="card-label">{title}</p>
      <strong className="card-value">{value}</strong>
      <div className="card-detail">{children}</div>
    </article>
  );
}
