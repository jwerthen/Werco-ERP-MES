import React, { useState, useEffect } from 'react';

// Components
import Hero from './components/Hero';
import Features from './components/Features';
import InteractiveDemo from './components/InteractiveDemo';
import PainPoints from './components/PainPoints';
import Compliance from './components/Compliance';
import Industries from './components/Industries';
import HowItWorks from './components/HowItWorks';
import Testimonials from './components/Testimonials';
import Pricing from './components/Pricing';
import CTA from './components/CTA';
import Footer from './components/Footer';
import Header from './components/Header';

export default function App() {
  const [isScrolled, setIsScrolled] = useState(false);

  useEffect(() => {
    const handleScroll = () => {
      setIsScrolled(window.scrollY > 50);
    };
    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  return (
    <div className="min-h-screen">
      <Header isScrolled={isScrolled} />
      <Hero />
      <Features />
      <InteractiveDemo />
      <PainPoints />
      <Compliance />
      <Industries />
      <HowItWorks />
      <Testimonials />
      <Pricing />
      <CTA />
      <Footer />
    </div>
  );
}
