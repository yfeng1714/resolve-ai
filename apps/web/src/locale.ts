export type Locale = "en" | "zh-CN";

const LOCALE_KEY = "resolveai_locale";

export function storedLocale(): Locale {
  try {
    return localStorage.getItem(LOCALE_KEY) === "zh-CN" ? "zh-CN" : "en";
  } catch {
    return "en";
  }
}

export function writeLocale(locale: Locale) {
  localStorage.setItem(LOCALE_KEY, locale);
}
