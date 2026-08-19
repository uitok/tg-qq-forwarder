import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react"
import { forwardRef } from "react"

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost" | "danger" }

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({ className = "", variant = "primary", ...props }, ref) {
  return <button ref={ref} className={`ui-button ui-button-${variant} ${className}`} {...props} />
})

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`ui-card ${className}`}>{children}</section>
}

export function CardHeader({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`ui-card-header ${className}`}>{children}</div>
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <h2 className="ui-card-title">{children}</h2>
}

export function CardDescription({ children }: { children: ReactNode }) {
  return <p className="ui-card-description">{children}</p>
}

export function CardContent({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`ui-card-content ${className}`}>{children}</div>
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className = "", ...props }, ref) {
  return <input ref={ref} className={`ui-input ${className}`} {...props} />
})

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className = "", ...props }, ref) {
  return <select ref={ref} className={`ui-input ui-select ${className}`} {...props} />
})

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "success" | "warning" | "danger" }) {
  return <span className={`ui-badge ui-badge-${tone}`}>{children}</span>
}

export function Tabs({ value, onValueChange, children }: { value: string; onValueChange: (value: string) => void; children: ReactNode }) {
  return <div className="tabs" data-value={value} data-change={onValueChange as unknown as string}>{children}</div>
}

export function TabsList({ children }: { children: ReactNode }) {
  return <div className="tabs-list">{children}</div>
}

export function TabsTrigger({ value, active, onClick, children }: { value: string; active: boolean; onClick: () => void; children: ReactNode }) {
  return <button type="button" className={`tabs-trigger ${active ? "tabs-trigger-active" : ""}`} onClick={onClick} aria-selected={active}>{children}</button>
}

export function Table({ children }: { children: ReactNode }) {
  return <div className="table-wrap"><table className="data-table">{children}</table></div>
}
