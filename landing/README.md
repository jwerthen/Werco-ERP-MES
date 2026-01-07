# Manufacturing ERP Landing Page

A professional, brand-neutral landing page for the ERP/MES SaaS platform.

## View the Landing Page

Simply open `index.html` in your web browser to view the complete landing page with all sections:
- Hero with customizable branding
- Features overview
- Interactive demo
- Pricing plans
- CTA sections
- And more!

## Project Structure

This landing page includes both:

1. **Standalone HTML Version** (`index.html`)
   - Ready to view immediately in any browser
   - Uses Tailwind CSS via CDN
   - Fully responsive
   - No build process required

2. **React + Vite Development Version** (in `src/` directory)
   - Full TypeScript implementation
   - Component-based architecture
   - Requires npm dependencies installation

## Development

### Using the React Version (Optional)

If you want to develop with React + Vite:

```bash
cd landing

# Note: npm install may have issues on some systems. If dependencies don't install,
# the standalone HTML version works perfectly without any installation.

npm install
npm run dev
```

The React version provides:
- Full TypeScript support
- Component reusability
- Development server with hot reload
- Production build optimization

## Features of the Landing Page

1. **Hero Section**
   - Compelling headline and value proposition
   - Compliance badges (AS9100D, ISO 9001, CMMC Level 2)
   - CTAs for trial and demo

2. **Features Overview**
   - 6 key feature cards
   - Highlights shop floor control, work orders, quality, compliance, etc.

3. **Interactive Demo**
   - Mock interface preview
   - Shows dashboard view
   - Demonstrates the intuitive UI

4. **Pricing Section**
   - Three pricing tiers (Starter, Professional, Enterprise)
   - Clear feature comparison
   - Monthly pricing with annual discount mention

5. **CTA Section**
   - Strong call-to-action
   - Emphasizes free trial
   - No credit card required

6. **Responsive Design**
   - Mobile-optimized
   - Touch-friendly interface
   - Works on all screen sizes

## Customization

The landing page is designed to be brand-neutral and easily customizable:

- Change colors in Tailwind configuration
- Update company name and branding
- Add your logo
- Customize pricing and features
- Add real testimonials
- Update contact information

## Deployment

### Static HTML Deployment

The standalone HTML version can be deployed anywhere:
- Netlify
- Vercel
- GitHub Pages
- Any static hosting service

Simply upload the `index.html` file and it will work immediately.

### React Production Build

After installing dependencies (if they install successfully):

```bash
npm run build
```

The production files will be in the `dist/` directory.

## Notes

- The npm installation may have issues on some systems due to npm version compatibility
- The standalone HTML version works perfectly without any dependencies
- Both versions contain the same content and sections
- The React version is ideal for ongoing development and maintenance
