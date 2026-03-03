terraform {
	required_providers {
		local =  {
			source = "hashicorp/local"
			version = "2.5.0"
			}
	}
}
provider "local" {}
resource "local_file" "test_txt" {
	filename = "${path.module}/test.txt"
	content = "Hello,world"
}
