# Sense Collector

![Sense Collector Header](docs/images/sense_collector_header.png)

**Sense Collector** is a Python-based application deployed via Docker that collects data from the [Sense](https://sense.com/) energy monitoring system. It enables real-time energy monitoring through a set of pre-built, configurable Grafana dashboards, giving you powerful insights into your energy consumption patterns.

## Key Features
- **Data Collection**: Automatically collects energy usage data from Sense energy monitors.
- **Grafana Dashboards**: Pre-configured dashboards for visualizing device-specific and whole-home energy usage data.
- **InfluxDB Integration**: Stores data in InfluxDB, offering a flexible and scalable way to manage energy metrics.
- **Easy Setup**: Deployed quickly using Docker with minimal configuration.

## Quick Start Guide

To get up and running with Sense Collector, you will need to:
1. Ensure you have **Docker**, **Docker Compose**, **InfluxDB 2.x**, and **Grafana** installed.
2. Pull the latest Sense Collector Docker image.
3. Configure your environment variables for Sense API credentials and InfluxDB connection.

For complete setup instructions, including all the necessary environment variables and deployment configurations, please visit the [Wiki](https://github.com/luxardolabs/sense-collector/wiki/Getting-Started).

## Dashboards

Sense Collector includes several Grafana dashboards to help you monitor your energy usage:
- **Device Overview**: Displays real-time wattage usage for individual devices.
- **Mains Overview**: Shows household voltage, frequency, and power consumption.
- **Monitor & Detection**: Visualizes Sense monitor detection status and Wi-Fi signal strength.

You can find detailed instructions on setting up and customizing these dashboards in the [Grafana Dashboards section](https://github.com/luxardolabs/sense-collector/wiki/Grafana-Dashboards) of the Wiki.

## Configuration

Sense Collector uses a set of environment variables to control its behavior and integrate with the Sense API and InfluxDB. These variables must be configured before deploying the container. For a full list of required and optional variables, visit the [Environment Variables page](https://github.com/luxardolabs/sense-collector/wiki/Environment-Variables).

## Support & Contact

Questions or need support? Please [open an issue](https://github.com/luxardolabs/sense-collector/issues) on the project repository.

Project Link: [https://github.com/luxardolabs/sense-collector](https://github.com/luxardolabs/sense-collector)

## Acknowledgements

- [Grafana Labs](https://grafana.com/)
- [InfluxData](https://www.influxdata.com/)
- [Sense Labs](https://sense.com/)

## License

This project is licensed under the [GNU Affero General Public License v3.0 (AGPL-3.0-only)](LICENSE).
