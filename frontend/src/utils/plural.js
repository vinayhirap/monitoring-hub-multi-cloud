// "1 alert" / "3 alerts": proper plurals instead of "alert(s)" in text people read.
export const plural = (n, word, many) => `${n} ${Number(n) === 1 ? word : (many || word + "s")}`;
export const pluralWord = (n, word, many) => (Number(n) === 1 ? word : (many || word + "s"));
