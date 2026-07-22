# Separate the web process from the persistent runtime

AgentDeck keeps the dashboard in a restartable Web Process and long-lived agent ownership in a
separate Persistent Runtime connected through a local control boundary. This preserves Active
Turns and Pending Interactions across ordinary web deployments, at the cost of two services,
explicit state projection, and a special continuation path when the runtime itself must restart.
