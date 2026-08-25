import { CALLISTO_BUILD_LABEL } from '../buildInfo'

export function BuildIdentity() {
  return (
    <div
      aria-label="Callisto build identity"
      className="flex flex-col justify-center leading-none"
    >
      <span className="text-sm font-bold text-green-400 tracking-wider font-data">
        CALLISTO
      </span>
      <span className="mt-0.5 text-[9px] font-medium tracking-[0.18em] text-muted-foreground/70 font-data">
        {CALLISTO_BUILD_LABEL}
      </span>
    </div>
  )
}
