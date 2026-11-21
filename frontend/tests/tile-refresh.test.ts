import { afterEach, describe, expect, it, vi } from 'vitest'
import { TileRefreshLoop } from '../modules/map-data/runtime/core/tile-refresh'

afterEach(() => vi.useRealTimers())
describe('catalog refresh lifecycle', () => {
  it('renews HMAC before expiry with polling disabled', async () => {
    vi.useFakeTimers(); vi.setSystemTime(0)
    const refresh = vi.fn(async () => { loop.configure(new Date(Date.now()+10000).toISOString(),0) })
    const loop = new TileRefreshLoop(refresh,vi.fn())
    loop.configure(new Date(10000).toISOString(),0)
    await vi.advanceTimersByTimeAsync(8000)
    expect(refresh).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(8000)
    expect(refresh).toHaveBeenCalledTimes(2)
    loop.destroy(); await vi.advanceTimersByTimeAsync(20000)
    expect(refresh).toHaveBeenCalledTimes(2)
  })
  it('coalesces a tile error storm into one flight and throttles repeats', async () => {
    vi.useFakeTimers(); vi.setSystemTime(0)
    let finish!: () => void
    const refresh = vi.fn(() => new Promise<void>(resolve=>{finish=resolve}))
    const loop = new TileRefreshLoop(refresh,vi.fn())
    const first = loop.trigger('retired')
    for(let i=0;i<100;i++) expect(loop.trigger('retired')).toBe(first)
    finish(); await first
    await loop.trigger('retired'); expect(refresh).toHaveBeenCalledTimes(1)
    loop.destroy()
  })
  it('retries transient failures with backoff but stops on auth failure', async () => {
    vi.useFakeTimers(); vi.setSystemTime(0)
    const error = vi.fn(), refresh = vi.fn(async () => { throw new Error('offline') })
    const loop = new TileRefreshLoop(refresh,error)
    await loop.trigger('manual')
    await vi.advanceTimersByTimeAsync(999); expect(refresh).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1); expect(refresh).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1999); expect(refresh).toHaveBeenCalledTimes(2)
    refresh.mockImplementation(async()=>{throw Object.assign(new Error('unauthorized'),{api:{status:401}})})
    await vi.advanceTimersByTimeAsync(1); expect(refresh).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(60000); expect(refresh).toHaveBeenCalledTimes(3)
    loop.destroy()
  })
  it('resumes from a sleeping tab and follows configured poll intervals', async () => {
    vi.useFakeTimers(); vi.setSystemTime(0)
    const refresh=vi.fn(async()=>{}),loop=new TileRefreshLoop(refresh,vi.fn())
    loop.configure(null,0); vi.setSystemTime(900000)
    await loop.trigger('focus'); expect(refresh).toHaveBeenCalledTimes(1)
    loop.configure(null,10)
    await vi.advanceTimersByTimeAsync(10000); expect(refresh).toHaveBeenCalledTimes(2)
    loop.pause(); await vi.advanceTimersByTimeAsync(10000); expect(refresh).toHaveBeenCalledTimes(2)
    loop.destroy()
  })
  it('never restarts timers or reports a late failure after disposal', async () => {
    vi.useFakeTimers(); vi.setSystemTime(0)
    let fail!: (error: Error) => void
    const refresh=vi.fn(()=>new Promise<void>((_,reject)=>{fail=reject})),error=vi.fn()
    const loop=new TileRefreshLoop(refresh,error)
    const flight=loop.trigger('manual')
    loop.destroy();fail(new Error('late response'));await flight
    loop.configure(new Date(10000).toISOString(),1);await loop.trigger('focus')
    await vi.advanceTimersByTimeAsync(60000)
    expect(refresh).toHaveBeenCalledTimes(1);expect(error).not.toHaveBeenCalled()
  })
})
