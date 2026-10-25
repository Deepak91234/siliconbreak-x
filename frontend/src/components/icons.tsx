/**
 * Inline stroke icons (24-unit grid, currentColor) so the UI needs no icon
 * dependency. Shapes follow the common 1.75-stroke outline style.
 */

import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: P & { children: React.ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.75}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden {...rest}>
      {children}
    </svg>
  );
}

export const IconShield = (p: P) => <Svg {...p}><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z" /></Svg>;
export const IconShieldCheck = (p: P) => <Svg {...p}><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z" /><path d="M8.8 12.2l2.2 2.2 4.3-4.6" /></Svg>;
export const IconSearch = (p: P) => <Svg {...p}><circle cx="11" cy="11" r="6.5" /><path d="M20 20l-4.2-4.2" /></Svg>;
export const IconBug = (p: P) => <Svg {...p}><rect x="7.5" y="8" width="9" height="12" rx="4.5" /><path d="M12 12v8M9.5 5.5L8 4M14.5 5.5L16 4M9 8a3 3 0 0 1 6 0M3.5 13h4M16.5 13h4M4.5 18.5l3.2-1.6M19.5 18.5l-3.2-1.6M4.5 7.5l3.2 1.6M19.5 7.5l-3.2 1.6" /></Svg>;
export const IconWrench = (p: P) => <Svg {...p}><path d="M14.7 6.3a4 4 0 0 0 5 5L21 12.6l-.1.1a6 6 0 0 1-7.6 1.3L6.5 20.8a2.1 2.1 0 0 1-3-3l6.8-6.8a6 6 0 0 1 1.3-7.6l.1-.1z" /></Svg>;
export const IconCheck = (p: P) => <Svg {...p}><path d="M5 12.5l4.5 4.5L19 7.5" /></Svg>;
export const IconCheckCircle = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M8.3 12.3l2.5 2.5 4.9-5.2" /></Svg>;
export const IconXCircle = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M9 9l6 6M15 9l-6 6" /></Svg>;
export const IconAlert = (p: P) => <Svg {...p}><path d="M10.3 4.2L2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z" /><path d="M12 9.5v4M12 17h.01" /></Svg>;
export const IconInfo = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 11v5.5M12 7.5h.01" /></Svg>;
export const IconChip = (p: P) => <Svg {...p}><rect x="6" y="6" width="12" height="12" rx="2" /><rect x="9.5" y="9.5" width="5" height="5" rx="1" /><path d="M9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5" /></Svg>;
export const IconCamera = (p: P) => <Svg {...p}><path d="M4.5 7.5h3l1.6-2.2h5.8l1.6 2.2h3a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18V9a1.5 1.5 0 0 1 1.5-1.5z" /><circle cx="12" cy="13" r="3.5" /></Svg>;
export const IconBars = (p: P) => <Svg {...p}><path d="M4 20h16" /><rect x="5.5" y="11" width="3" height="6.5" rx="0.8" /><rect x="10.5" y="6.5" width="3" height="11" rx="0.8" /><rect x="15.5" y="9" width="3" height="8.5" rx="0.8" /></Svg>;
export const IconCode = (p: P) => <Svg {...p}><path d="M8.5 7.5L4 12l4.5 4.5M15.5 7.5L20 12l-4.5 4.5M13.5 5l-3 14" /></Svg>;
export const IconLayers = (p: P) => <Svg {...p}><path d="M12 3.5l9 4.5-9 4.5-9-4.5z" /><path d="M3 12.5l9 4.5 9-4.5M3 16.5l9 4.5 9-4.5" /></Svg>;
export const IconTerminal = (p: P) => <Svg {...p}><rect x="3" y="4.5" width="18" height="15" rx="2" /><path d="M7 9.5l3 2.5-3 2.5M12.5 15H17" /></Svg>;
export const IconPlay = (p: P) => <Svg {...p}><path d="M8 5.5v13l10.5-6.5z" /></Svg>;
export const IconBolt = (p: P) => <Svg {...p}><path d="M13 2.5L4.5 13.5H12l-1 8 8.5-11H12z" /></Svg>;
export const IconDownload = (p: P) => <Svg {...p}><path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M4.5 19.5h15" /></Svg>;
export const IconExternal = (p: P) => <Svg {...p}><path d="M14 4.5h5.5V10M19.5 4.5L11 13M17 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 4 18.5v-10A1.5 1.5 0 0 1 5.5 7H10" /></Svg>;
export const IconSun = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4" /></Svg>;
export const IconMoon = (p: P) => <Svg {...p}><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></Svg>;
export const IconRefresh = (p: P) => <Svg {...p}><path d="M20 12a8 8 0 1 1-2.4-5.7M20 4.5v4.2h-4.2" /></Svg>;
export const IconSparkles = (p: P) => <Svg {...p}><path d="M10 3.5l1.6 4.4 4.4 1.6-4.4 1.6L10 15.5l-1.6-4.4L4 9.5l4.4-1.6zM17.5 14l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" /></Svg>;
export const IconLock = (p: P) => <Svg {...p}><rect x="5" y="10.5" width="14" height="10" rx="2" /><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" /></Svg>;
export const IconEye = (p: P) => <Svg {...p}><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle cx="12" cy="12" r="3" /></Svg>;
export const IconClock = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Svg>;
export const IconChevronRight = (p: P) => <Svg {...p}><path d="M9.5 6l6 6-6 6" /></Svg>;
export const IconChevronLeft = (p: P) => <Svg {...p}><path d="M14.5 6l-6 6 6 6" /></Svg>;
export const IconArrowLeft = (p: P) => <Svg {...p}><path d="M19 12H5M11 6l-6 6 6 6" /></Svg>;
export const IconArrowRight = (p: P) => <Svg {...p}><path d="M5 12h14M13 6l6 6-6 6" /></Svg>;
export const IconMonitor = (p: P) => <Svg {...p}><rect x="3" y="4" width="18" height="12.5" rx="2" /><path d="M12 16.5V20M8 20h8" /></Svg>;
export const IconPanel = (p: P) => <Svg {...p}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" /></Svg>;
export const IconMenu = (p: P) => <Svg {...p}><path d="M4 7h16M4 12h16M4 17h16" /></Svg>;
export const IconX = (p: P) => <Svg {...p}><path d="M6 6l12 12M18 6L6 18" /></Svg>;
export const IconGit = (p: P) => <Svg {...p}><circle cx="6.5" cy="6" r="2.2" /><circle cx="6.5" cy="18" r="2.2" /><circle cx="17.5" cy="9" r="2.2" /><path d="M6.5 8.2v7.6M17.5 11.2c0 3-3 4-8.6 5.2" /></Svg>;
export const IconFile = (p: P) => <Svg {...p}><path d="M14 3.5H7a1.5 1.5 0 0 0-1.5 1.5v14A1.5 1.5 0 0 0 7 20.5h10a1.5 1.5 0 0 0 1.5-1.5V8z" /><path d="M14 3.5V8h4.5M9 13h6M9 16.5h4" /></Svg>;
export const IconWave = (p: P) => <Svg {...p}><path d="M2.5 12h3l2-6 3 12 3-9 2 5 1.5-2h4.5" /></Svg>;
export const IconFlask = (p: P) => <Svg {...p}><path d="M9.5 3.5h5M10 3.5v6L4.8 18a1.8 1.8 0 0 0 1.6 2.5h11.2a1.8 1.8 0 0 0 1.6-2.5L14 9.5v-6M7 14.5h10" /></Svg>;
export const IconCube = (p: P) => <Svg {...p}><path d="M12 2.8l8 4.6v9.2l-8 4.6-8-4.6V7.4z" /><path d="M4 7.4l8 4.6 8-4.6M12 12v9.2" /></Svg>;
export const IconGrid = (p: P) => <Svg {...p}><rect x="4" y="4" width="6.5" height="6.5" rx="1.2" /><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.2" /><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.2" /><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.2" /></Svg>;
export const IconPlug = (p: P) => <Svg {...p}><path d="M9 3.5v4M15 3.5v4M6.5 7.5h11V11a5.5 5.5 0 0 1-11 0zM12 16.5v4" /></Svg>;
export const IconCoins = (p: P) => <Svg {...p}><ellipse cx="9" cy="7" rx="5.5" ry="2.5" /><path d="M3.5 7v4c0 1.4 2.5 2.5 5.5 2.5s5.5-1.1 5.5-2.5V7M3.5 11v4c0 1.4 2.5 2.5 5.5 2.5 1 0 2-.1 2.8-.4" /><ellipse cx="16" cy="14" rx="4.5" ry="2" /><path d="M11.5 14v3.5c0 1.1 2 2 4.5 2s4.5-.9 4.5-2V14" /></Svg>;
export const IconUser = (p: P) => <Svg {...p}><circle cx="12" cy="8.5" r="3.5" /><path d="M5 20a7 7 0 0 1 14 0" /></Svg>;
export const IconBot = (p: P) => <Svg {...p}><rect x="4.5" y="8" width="15" height="11" rx="3" /><path d="M12 4.5V8M9 13h.01M15 13h.01M9.5 16.5h5M2.5 12.5v2M21.5 12.5v2" /><circle cx="12" cy="3.8" r="1" /></Svg>;
export const IconCopy = (p: P) => <Svg {...p}><rect x="8.5" y="8.5" width="11" height="11" rx="2" /><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5" /></Svg>;
export const IconFingerprint = (p: P) => <Svg {...p}><path d="M6.8 18.5c-.8-1.8-1.3-3.9-1.3-6.5a6.5 6.5 0 0 1 11.6-4M18.4 10.6c.1.5.1.9.1 1.4 0 3.2-.6 5.7-1.6 7.5M9.2 20c-.6-1.8-.9-4.3-.9-8a3.7 3.7 0 0 1 7.4 0c0 3-.3 5.5-.9 7.6M12 12v1.5c0 2.3-.3 4.6-.8 6.5" /></Svg>;
export const IconHash = (p: P) => <Svg {...p}><path d="M9.5 3.5L7.5 20.5M16.5 3.5l-2 17M4.5 8.5h16M3.5 15.5h16" /></Svg>;
export const IconTarget = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="4.5" /><circle cx="12" cy="12" r="0.8" fill="currentColor" /></Svg>;
