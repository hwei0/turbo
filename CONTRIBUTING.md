# Contributing to TURBO

Thank you for your interest in contributing to TURBO! This document provides guidelines for contributing to the project.

## Getting Started

1. **Fork the repository** and clone your fork locally
2. **Set up your development environment** following the installation instructions in [README.md](README.md)
3. **Create a branch** for your changes: `git checkout -b feature/your-feature-name`

## Development Guidelines

### Code Style

**Python Code:**
- Follow [PEP 8](https://pep8.org/) style guidelines
- Use type hints where appropriate
- Add docstrings to functions and classes using Google-style format
- Maximum line length: 100 characters

**Rust Code:**
- Follow the official [Rust Style Guide](https://doc.rust-lang.org/style-guide/)
- Run `cargo fmt` before committing
- Run `cargo clippy` and address warnings

### Documentation

- Update documentation for any changed functionality
- Add docstrings/comments for new functions and modules
- Update the README.md or relevant docs/ files if you change:
  - System architecture
  - Configuration options
  - IPC protocols
  - Logging formats

## Submitting Changes

### Pull Request Process

1. **Update your fork** with the latest changes from the main repository
2. **Commit your changes** with clear, descriptive commit messages:
   ```
   Short (50 chars or less) summary

   More detailed explanation if needed. Wrap at 72 characters.
   Include the motivation for the change and contrast with previous behavior.
   ```
3. **Push to your fork** and create a pull request
4. **Fill out the PR template** completely, describing:
   - What problem does this solve?
   - How does it solve it?
   - Any potential side effects or breaking changes?
   - How did you test it?

### PR Review Process

- A maintainer will review your PR within a few days
- Address any requested changes
- Once approved, a maintainer will merge your PR

## Areas for Contribution

We welcome contributions in many areas:

### Feature Enhancements
- **Additional model backends**: Support for YOLO, Faster R-CNN, or other detection models
- **Transport layer improvements**: Alternative congestion control algorithms, TCP fallback
- **Bandwidth allocation policies**: New allocation strategies beyond LP-based optimization
- **Monitoring and visualization**: Enhanced dashboard features, additional metrics

### Performance Improvements
- **Latency optimization**: Reduce end-to-end latency in the inference pipeline
- **Throughput optimization**: Improve bandwidth utilization efficiency
- **Resource efficiency**: Reduce CPU/memory overhead

### Testing and Validation
- **Unit tests**: Increase test coverage for core components
- **Integration tests**: End-to-end testing scenarios
- **Benchmarks**: Performance characterization under different network conditions

### Documentation
- **Tutorials**: Step-by-step guides for common use cases
- **Deployment guides**: Instructions for cloud providers, edge devices
- **API documentation**: Comprehensive function/class documentation
- **Examples**: Additional example configurations and scenarios

### Bug Fixes
- Check the [Issues](https://github.com/NetSys/turbo/issues) page for known bugs
- Report new bugs with detailed reproduction steps

## Code of Conduct

### Our Standards

- Be respectful and inclusive
- Welcome newcomers and help them get started
- Provide constructive feedback
- Focus on what is best for the project and community

### Unacceptable Behavior

- Harassment, discrimination, or intimidation
- Trolling or insulting comments
- Publishing others' private information
- Other conduct inappropriate for a professional setting

## Getting Help

- **Questions about the code?** Open a [Discussion](https://github.com/NetSys/turbo/discussions)
- **Found a bug?** Open an [Issue](https://github.com/NetSys/turbo/issues)
- **Want to propose a feature?** Start a Discussion first to get feedback

## License

By contributing to this project, you agree that your contributions will be licensed under the project's license (see [LICENSE](LICENSE) file).

## Attribution

Contributors will be acknowledged in the project's README.md and release notes.

---

Thank you for contributing to TURBO!
