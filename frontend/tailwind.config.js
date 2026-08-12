/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ---------------------------------------------------------------
        // Semantic status colours. PRD §6 colour coding.
        //
        // These are the one part of the restyle that stayed high-contrast.
        // The red on a count-mismatch banner is a control-point signal, not
        // decoration — a guard misreading it is a safety failure rather than
        // an aesthetic one. Everything *around* them got the gradient
        // treatment; the signals themselves did not move.
        // ---------------------------------------------------------------
        ok: { DEFAULT: '#15803d', bg: '#dcfce7', dark: '#22c55e', darkbg: '#052e16' },
        bad: { DEFAULT: '#b91c1c', bg: '#fee2e2', dark: '#f87171', darkbg: '#450a0a' },
        warn: { DEFAULT: '#a16207', bg: '#fef9c3', dark: '#facc15', darkbg: '#422006' },
        info: { DEFAULT: '#1d4ed8', bg: '#dbeafe', dark: '#60a5fa', darkbg: '#172554' },

        // The gradient ramp from the reference design, as named stops so the
        // same wash can be reused rather than re-typed as hex in each place.
        wash: {
          50: '#ffffff',
          100: '#FFEDD5',
          200: '#FFDAB9',
          300: '#FFB6C1',
          400: '#E0BBE4',
          500: '#F3E5F5',
        },

        // shadcn's semantic surface tokens, driven by the CSS variables in
        // styles/index.css so a pasted-in shadcn component inherits this
        // theme instead of bringing its own.
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
      },
      fontSize: {
        // Base sizes stay one step up from Tailwind's defaults — PRD §6 asks
        // for large text readable under stress, and that survived the restyle.
        base: ['1.0625rem', { lineHeight: '1.6rem' }],
        lg: ['1.1875rem', { lineHeight: '1.75rem' }],
        xl: ['1.375rem', { lineHeight: '1.9rem' }],
      },
      spacing: {
        // Minimum comfortable touch target for a gloved hand on a tablet.
        touch: '3.5rem',
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 4px)',
        sm: 'calc(var(--radius) - 8px)',
      },
      backgroundImage: {
        // The reference design's wash, as one reusable layer stack.
        'hero-wash': `
          linear-gradient(180deg, #ffffff 0%, #FFEDD5 25%, #FFDAB9 50%, #FFB6C1 70%, #E0BBE4 85%, #F3E5F5 100%),
          radial-gradient(at 20% 30%, #ffffff33 0%, transparent 60%),
          radial-gradient(at 80% 70%, #f3e5f533 0%, transparent 70%)
        `,
        // Dark mode gets its own ramp rather than the pastel one dimmed. A
        // washed-out pastel on black reads as muddy grey; these are the same
        // hues at low luminance so the identity survives the theme switch.
        'hero-wash-dark': `
          linear-gradient(180deg, #0b0b12 0%, #17121c 30%, #21141f 55%, #2a1622 75%, #1d1526 90%, #0d0d14 100%),
          radial-gradient(at 20% 25%, #ffb6c11a 0%, transparent 55%),
          radial-gradient(at 80% 70%, #e0bbe41f 0%, transparent 65%)
        `,
      },
      boxShadow: {
        glass: '0 1px 0 0 rgba(255,255,255,0.6) inset, 0 8px 32px -8px rgba(15,23,42,0.18)',
        'glass-dark':
          '0 1px 0 0 rgba(255,255,255,0.06) inset, 0 8px 32px -8px rgba(0,0,0,0.6)',
      },
      keyframes: {
        'blur-in': {
          '0%': { opacity: '0', filter: 'blur(12px)', transform: 'translateY(12px)' },
          '100%': { opacity: '1', filter: 'blur(0)', transform: 'translateY(0)' },
        },
        'wash-drift': {
          '0%,100%': { transform: 'translate3d(0,0,0) scale(1)' },
          '50%': { transform: 'translate3d(0,-1.5%,0) scale(1.04)' },
        },
      },
      animation: {
        'pulse-fast': 'pulse 0.6s cubic-bezier(0.4, 0, 0.6, 1) 2',
        'blur-in': 'blur-in 0.7s cubic-bezier(0.16,1,0.3,1) both',
        'wash-drift': 'wash-drift 18s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
