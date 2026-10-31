const clock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** Format a server timestamp as Beijing time, "2026-10-29 14:30". */
export function formatTime(iso: string): string {
  const parts = Object.fromEntries(clock.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${parts["year"]}-${parts["month"]}-${parts["day"]} ${parts["hour"]}:${parts["minute"]}`;
}

/** Format a server timestamp as a Beijing calendar date, "2026-10-29". */
export function formatDate(iso: string): string {
  return formatTime(iso).slice(0, 10);
}
