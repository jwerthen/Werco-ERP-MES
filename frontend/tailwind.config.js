/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/**/*.{js,jsx,ts,tsx}",
    "./public/index.html"
  ],
  theme: {
    extend: {
      colors: {
        werco: {
          primary: '#1B4D9C',
          secondary: '#2563eb',
          accent: '#C8352B',
          success: '#10b981',
          warning: '#f59e0b',
          danger: '#C8352B',
        }
      }
    },
  },
  plugins: [],
}
