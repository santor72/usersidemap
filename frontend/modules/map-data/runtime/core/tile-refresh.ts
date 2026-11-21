/** One refresh flight and timer per map, including signed templates when poll=0. */
export class TileRefreshLoop {
  private timer?: ReturnType<typeof setTimeout>
  private flight?: Promise<void>
  private failures = 0
  private readonly lastReactive = new Map<string, number>()
  private stopped = false
  private disposed = false
  private expiry?: number
  private pollSeconds = 0
  constructor(private readonly refresh: () => Promise<void>, private readonly error: (error: unknown) => void) {}
  configure(expiry: string | null | undefined, pollSeconds: number) {
    if (this.disposed) return
    this.expiry = expiry ? Date.parse(expiry) : undefined
    this.pollSeconds = pollSeconds
    this.stopped = false
    this.schedule()
  }
  private schedule(retryDelay?: number) {
    clearTimeout(this.timer)
    if (this.stopped || this.disposed) return
    const remaining = this.expiry === undefined ? Infinity : this.expiry - Date.now()
    const expiryDelay = remaining === Infinity ? Infinity : remaining <= 0 ? 1000 : Math.max(250, remaining * .8)
    const delay = retryDelay ?? Math.min(expiryDelay, this.pollSeconds > 0 ? this.pollSeconds * 1000 : Infinity)
    if (Number.isFinite(delay)) this.timer = setTimeout(() => { void this.trigger('timer') }, Math.min(delay, 2147483647))
  }
  trigger(reason: 'timer' | 'focus' | 'retired' | 'expired' | 'manual'): Promise<void> {
    if (this.disposed) return Promise.resolve()
    if (this.flight) return this.flight
    if (this.stopped && reason !== 'manual' && reason !== 'focus') return Promise.resolve()
    if (reason !== 'manual' && reason !== 'timer' && Date.now() - (this.lastReactive.get(reason) ?? -Infinity) < 5000) return Promise.resolve()
    this.lastReactive.set(reason, Date.now())
    this.stopped = false
    clearTimeout(this.timer)
    this.flight = this.refresh().then(() => {
      this.failures = 0; this.schedule()
    }).catch(error => {
      if (this.disposed) return
      this.error(error)
      const status = (error as { api?: { status?: number } }).api?.status
      if (status && status < 500 && status !== 429) this.pause()
      else this.schedule(Math.min(30000, 1000 * 2 ** Math.min(this.failures++, 5)))
    }).finally(() => { this.flight = undefined })
    return this.flight
  }
  pause() { this.stopped = true; clearTimeout(this.timer) }
  destroy() { this.disposed = true; this.pause() }
}
