// Brand video on the sign-in page (audit G5): a 1.8 MB mp4 was downloaded by every visitor before they could even
// authenticate, including on phones where the panel it sits in is hidden by CSS (display: none does not stop a
// <video autoplay> from fetching). Load it only where it can be seen and is welcome.
export const WIDE_QUERY = "(min-width: 961px)";           // matches the .login-visual breakpoint in Login.css

/** @param {{wide:boolean, reducedMotion:boolean, saveData:boolean}} env */
export function shouldLoadBrandVideo({ wide, reducedMotion, saveData }) {
  return Boolean(wide) && !reducedMotion && !saveData;
}

export function readEnvironment(win = globalThis.window) {
  try {
    const mq = q => (typeof win.matchMedia === "function" ? win.matchMedia(q).matches : false);
    return {
      wide: mq(WIDE_QUERY),
      reducedMotion: mq("(prefers-reduced-motion: reduce)"),
      saveData: Boolean(win.navigator && win.navigator.connection && win.navigator.connection.saveData),
    };
  } catch {
    return { wide: false, reducedMotion: false, saveData: false };     // unknown: do not spend the bandwidth
  }
}
