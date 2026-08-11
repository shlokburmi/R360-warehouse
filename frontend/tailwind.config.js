/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // PRD §6 colour coding. Deliberately high-contrast: these are read
        // outdoors, in sunlight, by someone who is not looking for subtlety.
        ok: { DEFAULT: '#15803d', bg: '#dcfce7', dark: '#22c55e', darkbg: '#052e16' },
        bad: { DEFAULT: '#b91c1c', bg: '#fee2e2', dark: '#f87171', darkbg: '#450a0a' },
        warn: { DEFAULT: '#a16207', bg: '#fef9c3', dark: '#facc15', darkbg: '#422006' },
        info: { DEFAULT: '#1d4ed8', bg: '#dbeafe', dark: '#60a5fa', darkbg: '#172554' },
      },
      fontSize: {
        // Base sizes are one step up from Tailwind's defaults throughout the
        // app — PRD §6 asks for large text that is readable under stress.
        base: ['1.0625rem', { lineHeight: '1.6rem' }],
        lg: ['1.1875rem', { lineHeight: '1.75rem' }],
        xl: ['1.375rem', { lineHeight: '1.9rem' }],
      },
      spacing: {
        // Minimum comfortable touch target for a gloved hand on a tablet.
        touch: '3.5rem',
      },
      animation: {
        'pulse-fast': 'pulse 0.6s cubic-bezier(0.4, 0, 0.6, 1) 2',
      },
    },
  },
  plugins: [],
}
