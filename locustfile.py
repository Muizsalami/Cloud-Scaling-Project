from locust import HttpUser, task, between #importations

class WebsiteUser(HttpUser):
    wait_time = between(0, 0)

    @task
    def hit_homepage(self):
        self.client.get("/health")
